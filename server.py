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

@routes.post("/testVision")
async def testVision(request):
    if not xaiConfig:
        return web.json_response({"error": "XAI not configured"}, status=403)
    apiKey = xaiConfig.get("ApiKey", "")
    if not apiKey:
        return web.json_response({"error": "API key not configured"}, status=403)

    body = await request.json()
    image_base64 = body.get("image", "")
    if not image_base64:
        return web.json_response({"error": "No image provided"}, status=400)

    try:
        async with aiohttp.ClientSession() as vsession:
            async with vsession.post(
                "https://api.x.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {apiKey}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "grok-2-vision-1212",
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
                            {"type": "text", "text": "何が映っていますか？日本語で簡潔に説明してください。"}
                        ]
                    }]
                }
            ) as resp:
                result = await resp.json()
                return web.json_response(result)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


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
                "instructions": instructions + " You have a camera. When the conversation implies you should look at something (e.g. searching for objects, describing surroundings, identifying things), use the capture_image tool autonomously. Do not ask permission to look - just do it naturally.",
                "modalities": ["text", "audio"],
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": None,
                "input_audio_transcription": {
                    "model": "grok-2-latest"
                },
                "tools": [
                    {
                        "type": "function",
                        "name": "capture_image",
                        "description": "Capture a photo from Watney's onboard camera and analyze what is visible. Use this when the conversation requires visual information.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "reason": {
                                    "type": "string",
                                    "description": "Why you are looking (e.g. 'looking for a wallet', 'checking surroundings')"
                                }
                            },
                            "required": ["reason"]
                        }
                    }
                ]
            }
        })
        print(f"Voice chat: session.update sent")

        pending_snapshot = asyncio.Future()

        async def analyze_image(image_base64, reason):
            """Send image to Grok Vision API for analysis"""
            try:
                async with aiohttp.ClientSession() as vsession:
                    async with vsession.post(
                        "https://api.x.ai/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {apiKey}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "model": "grok-2-vision-1212",
                            "messages": [{
                                "role": "user",
                                "content": [
                                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
                                    {"type": "text", "text": f"あなたはWatneyというローバーロボットのカメラです。今カメラに映っているものを日本語で簡潔に説明してください。目的: {reason}"}
                                ]
                            }]
                        }
                    ) as resp:
                        result = await resp.json()
                        if "choices" in result:
                            return result["choices"][0]["message"]["content"]
                        else:
                            print(f"Vision API unexpected response: {result}")
                            return f"画像分析に失敗しました: {result.get('error', {}).get('message', 'unknown error')}"
            except Exception as e:
                print(f"Vision API error: {e}")
                return f"画像分析に失敗しました: {e}"

        async def browser_to_xai():
            nonlocal pending_snapshot
            async for msg in ws_browser:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    # Handle snapshot response from browser
                    if data.get("type") == "snapshot_result":
                        if not pending_snapshot.done():
                            pending_snapshot.set_result(data.get("image", ""))
                    elif not ws_xai.closed:
                        await ws_xai.send_str(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
            print("Voice chat: browser disconnected")

        async def xai_to_browser():
            nonlocal pending_snapshot
            async for msg in ws_xai:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)

                    # Intercept function calls
                    if data.get("type") == "response.function_call_arguments.done":
                        call_id = data.get("call_id")
                        func_name = data.get("name")
                        args = json.loads(data.get("arguments", "{}"))
                        print(f"Voice chat: function call '{func_name}' args={args}")

                        if func_name == "capture_image":
                            reason = args.get("reason", "observation")
                            # Request snapshot from browser
                            pending_snapshot = asyncio.get_event_loop().create_future()
                            await ws_browser.send_json({"type": "snapshot_request"})

                            try:
                                image_base64 = await asyncio.wait_for(pending_snapshot, timeout=5)
                                result = await analyze_image(image_base64, reason)
                            except asyncio.TimeoutError:
                                result = "カメラからの画像取得がタイムアウトしました"

                            print(f"Vision result: {result[:80]}...")
                            await ws_xai.send_json({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": result
                                }
                            })
                            await ws_xai.send_json({"type": "response.create"})

                    # Forward all messages to browser
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

    

