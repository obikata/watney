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
from apriltag_tracker import AprilTagTracker

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
aprilTagTracker = None

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
        if aprilTagTracker and aprilTagTracker.isRunning() and newBearing != "0":
            aprilTagTracker.onManualCommand()
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


_pendingSpeech = []  # ブラウザに再生させるキュー

def sayJapanese(text):
    """ブラウザで再生させるためにキューに入れる"""
    _pendingSpeech.append(text)
    print(f"TTS queued: '{text}'", flush=True)


@routes.post("/ttsSpeak")
async def ttsSpeak(request):
    """xAI TTS APIで音声生成してmp3をブラウザに返す"""
    import requests as req

    obj = await request.json()
    text = obj.get("text", "")
    if not text:
        return web.Response(status=400, text="No text")

    if not xaiConfig or not xaiConfig.get("ApiKey"):
        return web.Response(status=503, text="No API key")

    voice = xaiConfig.get("Voice", "Rex")
    try:
        resp = req.post(
            "https://api.x.ai/v1/tts",
            headers={
                "Authorization": f"Bearer {xaiConfig['ApiKey']}",
                "Content-Type": "application/json"
            },
            json={
                "text": text,
                "voice_id": voice.lower(),
                "language": "ja"
            },
            timeout=10
        )
        if resp.status_code == 200:
            print(f"TTS: '{text}'", flush=True)
            return web.Response(body=resp.content, content_type="audio/mpeg")
        else:
            print(f"TTS API error: {resp.status_code} {resp.text}", flush=True)
            return web.Response(status=500, text=resp.text)
    except Exception as e:
        print(f"TTS error: {e}", flush=True)
        return web.Response(status=500, text=str(e))


@routes.get("/ttsPending")
async def ttsPending(request):
    """ブラウザがポーリングして再生すべきテキストを取得"""
    global _pendingSpeech
    if _pendingSpeech:
        texts = _pendingSpeech[:]
        _pendingSpeech = []
        return web.json_response({"texts": texts})
    return web.json_response({"texts": []})


@routes.post("/setVolume")
async def setVolume(request):
    volumeObj = await request.json()
    volume = int(volumeObj['volume'])
    alsa.setVolume(volume)
    return web.Response(text="OK")


_batteryWarned = False

@routes.post("/voiceAgentLook")
async def voiceAgentLook(request):
    """カメラ画像をGrok Visionで分析"""
    import requests as req

    if not xaiConfig or not xaiConfig.get("ApiKey"):
        return web.json_response({"description": "APIキーがない"}, status=503)

    obj = await request.json()
    image_base64 = obj.get("image", "")
    question = obj.get("question", "何が見える？")

    try:
        resp = req.post(
            "https://api.x.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {xaiConfig['ApiKey']}",
                "Content-Type": "application/json"
            },
            json={
                "model": "grok-4-fast",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
                        {"type": "text", "text": f"あなたはワトニーというロボットローバーです。カメラで見えているものを日本語で短く（20文字以内）答えてください。質問：{question}"}
                    ]
                }],
                "temperature": 0.7
            },
            timeout=15
        )
        result = resp.json()
        description = result["choices"][0]["message"]["content"].strip()
        print(f"Vision: '{description}'", flush=True)
        return web.json_response({"description": description})
    except Exception as e:
        print(f"Vision error: {e}", flush=True)
        return web.json_response({"description": "よく見えなかった"})


@routes.get("/getApiKey")
async def getApiKey(request):
    """ローカルネットワーク内でのみAPIキーを提供"""
    if xaiConfig and xaiConfig.get("ApiKey"):
        return web.json_response({"key": xaiConfig["ApiKey"]})
    return web.json_response({"key": ""}, status=503)


@routes.post("/voiceAgentAction")
async def voiceAgentAction(request):
    """Voice Agentのfunction callを実行"""
    import threading, time as _time

    obj = await request.json()
    action = obj.get("action")
    args = obj.get("args", {})

    print(f"Voice Agent action: {action} {args}", flush=True)

    if action == "move":
        direction_map = {"forward": "n", "backward": "s", "left": "w", "right": "e", "stop": "0"}
        bearing = direction_map.get(args.get("direction"), "0")
        motorController.setBearing(bearing, True)

    elif action == "spin":
        import random
        speed = 60
        if random.choice([True, False]):
            motorController.setMotorDC(speed, -speed)
        else:
            motorController.setMotorDC(-speed, speed)

    elif action == "patrol":
        if aprilTagTracker:
            if args.get("action") == "start":
                aprilTagTracker.startPatrol([3, 8, 11, 13])
            else:
                aprilTagTracker.stop()

    elif action == "lights":
        pass  # TODO: toggle lights

    return web.json_response({"status": "ok"})


@routes.post("/heartbeat")
async def onHeartbeat(request):
    global _batteryWarned
    stats = heartbeat.onHeartbeatReceived()
    # バッテリー低下通知
    batteryPercent = stats.get("BatteryPercent", -1)
    if batteryPercent >= 0 and batteryPercent <= 20 and not _batteryWarned:
        _batteryWarned = True
        sayJapanese("ワトニーそろそろ疲れてきたよ。充電して！")
    elif batteryPercent > 30:
        _batteryWarned = False
    if aprilTagTracker:
        stats["AprilTagTracking"] = aprilTagTracker.isRunning()
        stats["AprilTagDetection"] = aprilTagTracker.getLastDetection()
        status = aprilTagTracker.getStatus()
        stats["AprilTagPatrol"] = status if (status["patrolling"] or status["completed"]) else None
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


@routes.post("/apriltagTrack")
async def apriltagTrack(request):
    trackObj = await request.json()
    enable = bool(trackObj.get('enable', False))
    if aprilTagTracker is None:
        return web.Response(status=503, text="AprilTag tracker not available")
    if enable:
        aprilTagTracker.start()
    else:
        aprilTagTracker.stop()
    return web.json_response({"tracking": aprilTagTracker.isRunning()})


@routes.post("/apriltagPatrol")
async def apriltagPatrol(request):
    if aprilTagTracker is None:
        return web.Response(status=503, text="AprilTag tracker not available")
    patrolObj = await request.json()
    enable = bool(patrolObj.get('enable', False))
    if enable:
        route = patrolObj.get('route', [3, 13, 8])
        aprilTagTracker.startPatrol(route)
    else:
        aprilTagTracker.stop()
    return web.json_response(aprilTagTracker.getStatus())


@routes.post("/apriltagFrame")
async def apriltagFrame(request):
    if aprilTagTracker is None or not aprilTagTracker.isRunning():
        return web.Response(status=200, text="Not tracking")
    frameObj = await request.json()
    imageBase64 = frameObj.get('image', '')
    if imageBase64:
        aprilTagTracker.processFrame(imageBase64)
    return web.Response(text="OK")


@routes.post("/voiceCommand")
async def voiceCommand(request):
    """Receive audio from browser, send to xAI STT, execute command."""
    global xaiConfig
    if not xaiConfig or not xaiConfig.get("ApiKey"):
        return web.json_response({"error": "xAI API key not configured"}, status=503)

    import requests as req
    import tempfile

    reader = await request.multipart()
    field = await reader.next()
    audio_data = await field.read()

    # Send to xAI STT
    try:
        resp = req.post(
            "https://api.x.ai/v1/stt",
            headers={"Authorization": f"Bearer {xaiConfig['ApiKey']}"},
            files={"file": ("audio.webm", audio_data, "audio/webm")},
            data=[("language", "ja")]
        )
        result = resp.json()
        transcript = result.get("text", "").strip()
        print(f"Voice command: '{transcript}'", flush=True)
    except Exception as e:
        print(f"STT error: {e}", flush=True)
        return web.json_response({"error": str(e)}, status=500)

    # コマンド判定（Grok APIで意図を解釈）
    action = await interpretCommand(transcript, xaiConfig['ApiKey'])
    if action:
        executeVoiceAction(action)

    return web.json_response({"transcript": transcript, "action": action})


async def interpretCommand(text, apiKey):
    """Grok APIでユーザーの意図を解釈してコマンドを返す"""
    import requests as req

    valid_actions = ["n", "s", "e", "w", "0", "patrol_start", "patrol_stop", "lights", "greet", "spin", "none"]

    prompt = f"""あなたはロボットローバー「ワトニー」の音声コマンドインタープリタです。
音声認識の結果を受け取り、ユーザーの意図を推測してコマンドを1つだけ返してください。
音声認識は不正確な場合があります（例:「前進」→「全身」、「停止」→「投資」など）。
音が似ている言葉は元の意図を推測してください。

コマンドリスト:
- "n" : 前に進む（前、進め、前進、全身、行け、ゴー、go）
- "s" : 後ろに下がる（後ろ、バック、下がれ、後退）
- "e" : 右に曲がる（右、右折、ライト方向ではない）
- "w" : 左に曲がる（左、左折）
- "0" : 停止する（止まれ、ストップ、止まって、止めて）
- "patrol_start" : パトロールを開始する（パトロール、巡回開始、見回り、見回って）
- "patrol_stop" : パトロールを停止する（パトロール停止、巡回やめて、巡回止めて、もういい）
- "lights" : ライトのON/OFF（ライト、電気、明かり、照明）
- "greet" : ワトニーへの呼びかけ・挨拶（ワトニー、おーい、ハロー、元気？、こんにちは）
- "spin" : その場でぐるぐる回る（ぐるぐる、回って、回れ、スピン）
- "none" : どのコマンドにも該当しない

ユーザーの発話（音声認識結果）: 「{text}」

コマンドのみを返してください（例: n）:"""

    try:
        resp = req.post(
            "https://api.x.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {apiKey}",
                "Content-Type": "application/json"
            },
            json={
                "model": "grok-3-mini-fast",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0
            },
            timeout=5
        )
        result = resp.json()
        answer = result["choices"][0]["message"]["content"].strip().lower()
        # 有効なアクションのみ返す
        if answer in valid_actions and answer != "none":
            print(f"Grok interpreted '{text}' → {answer}", flush=True)
            return answer
        print(f"Grok interpreted '{text}' → none", flush=True)
        return None
    except Exception as e:
        print(f"Grok API error: {e}", flush=True)
        # フォールバック: キーワードマッチ
        return matchVoiceCommand(text)


def matchVoiceCommand(text):
    """日本語テキストをコマンドにマッピング（長いキーワードを優先）"""
    text = text.lower().strip()
    # (keyword, action) のリスト - 長いものを先にマッチさせる
    keywords = [
        # パトロール（停止を先に判定）
        ("パトロール停止", "patrol_stop"),
        ("パトロールストップ", "patrol_stop"),
        ("ぱとろーる停止", "patrol_stop"),
        ("パトロール止めて", "patrol_stop"),
        ("パトロールやめて", "patrol_stop"),
        ("パトロール開始", "patrol_start"),
        ("パトロールスタート", "patrol_start"),
        ("ぱとろーる開始", "patrol_start"),
        ("パトロール", "patrol_start"),
        # 移動
        ("前に進め", "n"),
        ("前進", "n"),
        ("ぜんしん", "n"),
        ("進め", "n"),
        ("すすめ", "n"),
        ("前", "n"),
        ("まえ", "n"),
        ("後ろ", "s"),
        ("うしろ", "s"),
        ("バック", "s"),
        ("ばっく", "s"),
        ("後退", "s"),
        ("右", "e"),
        ("みぎ", "e"),
        ("左", "w"),
        ("ひだり", "w"),
        # 停止
        ("止まれ", "0"),
        ("とまれ", "0"),
        ("ストップ", "0"),
        ("すとっぷ", "0"),
        ("止まって", "0"),
        ("停止", "0"),
        ("ていし", "0"),
        ("止まる", "0"),
        # ライト
        ("ライトつけて", "lights"),
        ("電気つけて", "lights"),
        ("電気消して", "lights"),
        ("ライト", "lights"),
        ("らいと", "lights"),
        ("電気", "lights"),
        ("でんき", "lights"),
    ]
    for kw, action in keywords:
        if kw in text:
            return action
    return None


def executeVoiceAction(action):
    """コマンドを実行"""
    global motorController, aprilTagTracker
    if action in ("n", "s", "e", "w", "ne", "nw", "se", "sw"):
        motorController.setBearing(action, True)
        # 1秒後に停止
        import threading
        def stopAfter():
            import time
            time.sleep(1.0)
            motorController.setBearing("0", False)
        threading.Thread(target=stopAfter, daemon=True).start()
    elif action == "0":
        motorController.setBearing("0", False)
    elif action == "patrol_start":
        if aprilTagTracker:
            aprilTagTracker.startPatrol([3, 8, 11, 13])
    elif action == "patrol_stop":
        if aprilTagTracker:
            aprilTagTracker.stop()
    elif action == "lights":
        # Toggle lights via existing mechanism
        pass
    elif action == "greet":
        import random
        greetings = ["はーい", "なあに？", "ここだよ！", "ワトニーだよ！", "よんだ？"]
        sayJapanese(random.choice(greetings))
    elif action == "spin":
        import threading
        sayJapanese("ぐるぐるー！")
        def doSpin():
            import time
            motorController.setMotorDC(60, -60)
            time.sleep(3.0)
            motorController.setMotorDC(0, 0)
        threading.Thread(target=doSpin, daemon=True).start()


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
                            "model": "grok-4-1-fast-non-reasoning",
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

    if config.has_section("APRILTAG"):
        try:
            aprilTagTracker = AprilTagTracker(config, motorController)
            print("AprilTag tracker initialized")
        except Exception as e:
            print(f"AprilTag tracker not available: {e}")
            aprilTagTracker = None

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
        if aprilTagTracker and aprilTagTracker.isRunning():
            aprilTagTracker.stop()
        servoController.stop()
        lightsController.stop()
        gpio.stop()
        janus.endProcess()
        videoStream.endProcess()
        for runner in runners:
            loop.run_until_complete(runner.cleanup())

    

