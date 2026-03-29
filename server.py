from audiomanager import AudioManager
from offcharger import OffCharger
from powerplant import PowerPlant
from aiohttp import web
from motorcontroller import MotorController
from servocontroller import ServoController
from lightscontroller import LightsController
from heartbeat import Heartbeat
from subprocess import call
import os
import pigpio
from configparser import ConfigParser
from alsa import Alsa
import ssl
import sys
from externalrunner import ExternalProcess
import asyncio
import aiohttp
import json
from januseventhandler import JanusEventHandler
from tts import TTSSpeaker
from startupSequence import StartupSequenceController

routes = web.RouteTableDef()

motorController = None
servoController = None
heartbeat = None
signalingServer = None
alsa = None
tts = None
powerPlant = None
startupController = None
audioManager = None
audioManagerThread = None
janusEventHandler = None
offCharger = None
xaiConfig = None

@routes.get("/")
async def getPageHTML(request):
    return web.FileResponse("index.html")


@routes.post("/sendCommand")
async def setCommand(request):
    commandObj = await request.json()
    newBearing = commandObj['bearing']
    newLook = commandObj['look']
    newSlow = commandObj['slow']

    if newBearing in MotorController.validBearings:
        motorController.setBearing(newBearing, newSlow)
    else:
        print("Invalid bearing {}".format(newBearing))
        return web.Response(status=400, text="Invalid")

    if newLook == 0:
        await servoController.lookStop()
    elif newLook == -1:
        await servoController.forward()
    elif newLook == 1:
        await servoController.backward()
    else:
        print("Invalid look at {}".format(newLook))
        return web.Response(status=400, text="Invalid")

    return web.Response(text="OK")


@routes.post("/shutDown")
async def shutDown(request):
    call("sudo halt", shell=True)

@routes.post("/restart")
async def restart(request):
    call("sudo reboot", shell=True)


@routes.post("/sendTTS")
async def sendTTS(request):
    ttsObj = await request.json()
    ttsString = ttsObj['str']
    tts.sayText(ttsString)
    return web.Response(text="OK")


@routes.post("/setVolume")
async def setVolume(request):
    volumeObj = await request.json()
    volume = int(volumeObj['volume'])
    alsa.setVolume(volume)
    return web.Response(text="OK")


@routes.post("/heartbeat")
async def onHeartbeat(request):
    stats = heartbeat.onHeartbeatReceived()
    return web.json_response(stats)

@routes.post("/lights")
async def onLights(request):
    lightsObj = await request.json()
    on = bool(lightsObj['on'])
    if on:
        lightsController.lightsOn()
    else:
        lightsController.lightsOff()
    return web.Response(text="OK")

@routes.get("/voiceChat")
async def voiceChatProxy(request):
    if not xaiConfig or not xaiConfig.getboolean("Enabled", fallback=False):
        return web.Response(status=403, text="Voice chat not enabled")

    apiKey = xaiConfig.get("ApiKey", "")
    if not apiKey:
        return web.Response(status=403, text="API key not configured")

    voice = xaiConfig.get("Voice", "Talia")
    instructions = xaiConfig.get("Instructions", "You are Watney, a friendly rover.")

    ws_browser = web.WebSocketResponse()
    await ws_browser.prepare(request)

    session = None
    ws_xai = None
    try:
        session = aiohttp.ClientSession()
        print(f"Voice chat: connecting to xAI...")
        ws_xai = await session.ws_connect(
            "wss://api.x.ai/v1/realtime",
            headers={"Authorization": f"Bearer {apiKey}"}
        )
        print(f"Voice chat: connected to xAI")

        await ws_xai.send_json({
            "type": "session.update",
            "session": {
                "voice": voice,
                "instructions": instructions,
                "modalities": ["text", "audio"],
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": None,
                "input_audio_transcription": {
                    "model": "grok-2-latest"
                }
            }
        })
        print(f"Voice chat: session.update sent")

        async def browser_to_xai():
            async for msg in ws_browser:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if not ws_xai.closed:
                        await ws_xai.send_str(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
            print("Voice chat: browser disconnected")

        async def xai_to_browser():
            async for msg in ws_xai:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if not ws_browser.closed:
                        await ws_browser.send_str(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    print(f"Voice chat: xAI connection closed/error: {msg.type}")
                    break
            print("Voice chat: xAI disconnected")

        await asyncio.gather(browser_to_xai(), xai_to_browser())

    except Exception as e:
        print(f"Voice chat error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if ws_xai and not ws_xai.closed:
            await ws_xai.close()
        if session:
            await session.close()
        if not ws_browser.closed:
            await ws_browser.close()

    return ws_browser


async def onJanusEvent(request):
    try:
        eventObj = await request.json()
        janusEventHandler.handleEvent(eventObj)
    except Exception as e:
        print('Error handling janus event: {}'.format(e))
    finally:
        return web.Response(text="OK")

# Python 3.7 is overly wordy about self-signed certificates, so we'll suppress the error here
def loopExceptionHandler(loop, context):
    exception = context.get('exception')
    if isinstance(exception, ssl.SSLError) and exception.reason == 'SSLV3_ALERT_CERTIFICATE_UNKNOWN':
        pass
    else:
        loop.default_exception_handler(context)


def createSSLContext(homePath):
    # Create an SSL context to be used by the websocket server
    print('Using TLS with keys in {!r}'.format(homePath))
    chain_pem = os.path.join(homePath, 'cert.pem')
    key_pem = os.path.join(homePath, 'key.pem')
    sslctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

    try:
        sslctx.load_cert_chain(chain_pem, keyfile=key_pem)
    except FileNotFoundError:
        print("Certificates not found, did you run generate_cert.sh?")
        sys.exit(1)
    return sslctx

runners = []
async def start_site(app, address, port, sslContext=None):
    runner = web.AppRunner(app)
    runners.append(runner)
    await runner.setup()

    if sslContext is not None:
        site = web.TCPSite(runner, host=address, port=port, ssl_context=sslContext)
    else:
        site = web.TCPSite(runner, host=address, port=port)
    await site.start()


if __name__ == "__main__":
    homePath = os.path.dirname(os.path.abspath(__file__))
    sslctx = createSSLContext(os.path.dirname(homePath))

    gpio = pigpio.pi()
    if not gpio.connected:
        print('GPIO not connected')
        exit()
    
    config = ConfigParser()
    config.read(os.path.join(homePath, "rover.conf"))
    audioConfig = config["AUDIO"]
    videoConfig = config["VIDEO"]
    xaiConfig = config["XAI"] if config.has_section("XAI") else None

    loop = asyncio.get_event_loop()
    loop.set_exception_handler(loopExceptionHandler)

    audioManager = AudioManager(config)

    motorController = MotorController(config, gpio, audioManager)

    alsa = Alsa(gpio, config)

    servoController = ServoController(gpio, config, audioManager)
    lightsController = LightsController(gpio, config)

    tts = TTSSpeaker(config, alsa, audioManager)

    try:
        powerPlant = PowerPlant(config)
    except OSError:
        print("PowerPlant not found on I2C bus - running without battery monitoring")
        powerPlant = None

    startupController = StartupSequenceController(config, servoController, lightsController, tts)

    heartbeat = Heartbeat(config, servoController, motorController, alsa, lightsController, powerPlant)

    offCharger = OffCharger(config, tts, motorController)

    janus = ExternalProcess(videoConfig["JanusStartCommand"], False, False, "janus.log")
    videoStream = ExternalProcess(videoConfig["GStreamerStartCommand"], True, False, "video.log")

    janusEventHandler = JanusEventHandler()

    mainApp = web.Application()
    mainApp.add_routes(routes)
    mainApp.router.add_static('/js/', path=os.path.join(homePath, 'js'))
    loop.create_task(start_site(mainApp, '0.0.0.0', 5000, sslctx))

    eventListenerApp = web.Application()
    eventListenerApp.add_routes([web.post('/janusEvent', onJanusEvent)])
    loop.create_task(start_site(eventListenerApp, 'localhost', 5001))

    try:
        loop.run_forever()
    except:
        pass
    finally:
        servoController.stop()
        lightsController.stop()
        gpio.stop()
        janus.endProcess()
        videoStream.endProcess()
        for runner in runners:
            loop.run_until_complete(runner.cleanup())

    

