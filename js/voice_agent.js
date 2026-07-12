// Voice Agent - Push-to-Talk + Function Calling
// G: 起動/停止, Space長押し: マイクON → Voice Agentにリアルタイム送信
(function () {
    var ws = null;
    var voiceAgentActive = false;
    var audioContext = null;
    var mediaStream = null;
    var audioProcessor = null;
    var audioBuffer = [];
    var micSending = false;
    var micInterval = null;
    var audioSent = false;
    var nextPlayTime = 0;
    var voiceOverlay = null;
    var chatLog = null;
    var apiKey = null;

    var SAMPLE_RATE = 24000;

    // --- UI ---
    function ensureOverlay() {
        if (voiceOverlay) return;
        voiceOverlay = document.createElement("div");
        voiceOverlay.id = "voiceAgentOverlay";
        voiceOverlay.style.cssText = "position:fixed;bottom:60px;left:50%;transform:translateX(-50%);z-index:9999;padding:10px 20px;border-radius:8px;background:rgba(0,0,0,0.8);color:white;font-size:16px;font-family:sans-serif;text-align:center;pointer-events:none;display:none;";
        document.body.appendChild(voiceOverlay);
    }

    function ensureChatLog() {
        if (chatLog) return;
        chatLog = document.createElement("div");
        chatLog.id = "voiceAgentChat";
        chatLog.style.cssText = "position:fixed;bottom:60px;right:10px;z-index:9999;width:320px;max-height:400px;overflow-y:auto;background:rgba(0,0,0,0.85);border-radius:8px;padding:10px;font-family:sans-serif;font-size:14px;color:white;display:none;";
        document.body.appendChild(chatLog);
    }

    function addChatMessage(who, text) {
        ensureChatLog();
        chatLog.style.display = "block";
        var msg = document.createElement("div");
        msg.style.cssText = "margin-bottom:6px;padding:4px 8px;border-radius:4px;word-wrap:break-word;";
        if (who === "user") {
            msg.style.background = "rgba(100,100,255,0.3)";
            msg.innerHTML = "<b style='color:#8af'>しゅんちゃん:</b> " + text;
        } else {
            msg.style.background = "rgba(0,180,0,0.2)";
            msg.innerHTML = "<b style='color:#0f0'>ワトニー:</b> " + text;
        }
        chatLog.appendChild(msg);
        chatLog.scrollTop = chatLog.scrollHeight;
        while (chatLog.children.length > 20) {
            chatLog.removeChild(chatLog.firstChild);
        }
    }

    function showStatus(text, color) {
        ensureOverlay();
        voiceOverlay.textContent = text;
        voiceOverlay.style.color = color || "white";
        voiceOverlay.style.display = "block";
    }

    function hideStatus() {
        if (voiceOverlay) voiceOverlay.style.display = "none";
    }

    function showStatusTemp(text, color, ms) {
        showStatus(text, color);
        setTimeout(hideStatus, ms || 2000);
    }

    function updateButton() {
        var btn = document.getElementById("voiceCommandButton");
        if (!btn) return;
        var icon = btn.querySelector("i");
        if (voiceAgentActive) {
            icon.textContent = "hearing";
            btn.style.color = "#0f0";
        } else {
            icon.textContent = "hearing_disabled";
            btn.style.color = "";
        }
    }

    // --- Audio utilities ---
    function float32ToBase64PCM16(float32Array) {
        var pcm16 = new Int16Array(float32Array.length);
        for (var i = 0; i < float32Array.length; i++) {
            var s = Math.max(-1, Math.min(1, float32Array[i]));
            pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        }
        var bytes = new Uint8Array(pcm16.buffer);
        var binary = "";
        for (var j = 0; j < bytes.length; j++) {
            binary += String.fromCharCode(bytes[j]);
        }
        return btoa(binary);
    }

    function base64PCM16ToFloat32(base64String) {
        var binaryString = atob(base64String);
        var bytes = new Uint8Array(binaryString.length);
        for (var i = 0; i < binaryString.length; i++) {
            bytes[i] = binaryString.charCodeAt(i);
        }
        var pcm16 = new Int16Array(bytes.buffer);
        var float32 = new Float32Array(pcm16.length);
        for (var j = 0; j < pcm16.length; j++) {
            float32[j] = pcm16[j] / 32768.0;
        }
        return float32;
    }

    // --- Audio playback ---
    var playingSources = [];

    function playAudioDelta(base64Audio) {
        if (!audioContext) return;
        var float32Data = base64PCM16ToFloat32(base64Audio);
        var buffer = audioContext.createBuffer(1, float32Data.length, SAMPLE_RATE);
        buffer.getChannelData(0).set(float32Data);
        var source = audioContext.createBufferSource();
        source.buffer = buffer;
        source.connect(audioContext.destination);
        var currentTime = audioContext.currentTime;
        var startTime = Math.max(currentTime, nextPlayTime);
        source.start(startTime);
        nextPlayTime = startTime + buffer.duration;
        playingSources.push(source);
        source.onended = function () {
            var idx = playingSources.indexOf(source);
            if (idx !== -1) playingSources.splice(idx, 1);
        };
    }

    function stopPlayback() {
        for (var i = 0; i < playingSources.length; i++) {
            try { playingSources[i].stop(); } catch(e) {}
        }
        playingSources = [];
        nextPlayTime = 0;
    }

    // --- Mic streaming to Voice Agent ---
    function startMicStream() {
        micInterval = setInterval(function () {
            if (audioBuffer.length === 0 || !ws || !micSending) {
                if (!micSending) audioBuffer = [];
                return;
            }
            var totalLength = 0;
            for (var i = 0; i < audioBuffer.length; i++) totalLength += audioBuffer[i].length;
            var combined = new Float32Array(totalLength);
            var offset = 0;
            for (var j = 0; j < audioBuffer.length; j++) {
                combined.set(audioBuffer[j], offset);
                offset += audioBuffer[j].length;
            }
            audioBuffer = [];
            ws.send(JSON.stringify({
                type: "input_audio_buffer.append",
                audio: float32ToBase64PCM16(combined)
            }));
            audioSent = true;
        }, 100);
    }

    function stopMicStream() {
        if (micInterval) { clearInterval(micInterval); micInterval = null; }
    }

    var micStartTime = 0;
    var MIN_TALK_MS = 300;

    function startTalking() {
        if (!voiceAgentActive || !ws) return;
        // 前のレスポンスをキャンセルして音声を止める
        ws.send(JSON.stringify({ type: "response.cancel" }));
        stopPlayback();
        muteResponseId = null;
        pendingMute = false;
        micSending = true;
        micStartTime = Date.now();
        audioBuffer = [];
        audioSent = false;
        showStatus("🎤 ...", "#f44");
    }

    function stopTalking() {
        if (!micSending) return;
        micSending = false;
        hideStatus();
        var held = Date.now() - micStartTime;
        // 短すぎる押下や無音は無視
        if (ws && audioSent && held >= MIN_TALK_MS) {
            ws.send(JSON.stringify({ type: "input_audio_buffer.commit" }));
            ws.send(JSON.stringify({ type: "response.create" }));
        } else if (ws) {
            ws.send(JSON.stringify({ type: "input_audio_buffer.clear" }));
        }
    }

    // --- WebSocket ---
    function connect() {
        if (!apiKey) return;
        ws = new WebSocket("wss://api.x.ai/v1/realtime?model=grok-voice-latest",
            ["xai-client-secret." + apiKey]);

        ws.onopen = function () {
            ws.send(JSON.stringify({
                type: "session.update",
                session: {
                    voice: "ani",
                    instructions: "あなたはワトニー。ロボット。一人称は「ワトニー」だが名乗らなくていい。日本語で短く返事（5文字以内）。毎回の指示を独立して処理しろ。前の指示に引きずられるな。「とまれ」「ストップ」は必ずstopとして処理。移動・回転・パトロール・カメラの指示は必ず対応するツールを呼べ。ツールを呼ばずに口で返事するだけは禁止。",
                    turn_detection: null,
                    input_audio_transcription: { language: "ja" },
                    audio: {
                        input: { format: { type: "audio/pcm", rate: SAMPLE_RATE } },
                        output: { format: { type: "audio/pcm", rate: SAMPLE_RATE } }
                    },
                    tools: [
                        {
                            type: "function",
                            name: "move",
                            description: "ローバー移動。「ぜんしん/全身/前/すすんで」=forward、「うしろ/バック」=backward、「みぎ」=right、「ひだり」=left、「とまれ/ストップ」=stop",
                            parameters: {
                                type: "object",
                                properties: {
                                    direction: { type: "string", enum: ["forward", "backward", "left", "right", "stop"] }
                                },
                                required: ["direction"]
                            }
                        },
                        {
                            type: "function",
                            name: "spin",
                            description: "ぐるぐる回る",
                            parameters: { type: "object", properties: {} }
                        },
                        {
                            type: "function",
                            name: "patrol",
                            description: "パトロール開始/停止",
                            parameters: {
                                type: "object",
                                properties: {
                                    action: { type: "string", enum: ["start", "stop"] }
                                },
                                required: ["action"]
                            }
                        },
                        {
                            type: "function",
                            name: "look",
                            description: "カメラで見えるものを確認。「何が見える」「前に何がある」",
                            parameters: {
                                type: "object",
                                properties: {
                                    question: { type: "string" }
                                }
                            }
                        }
                    ]
                }
            }));
            startMicStream();
            showStatusTemp("準備OK (Space長押しで話す)", "#0f0", 3000);
        };

        ws.onmessage = function (event) {
            var msg = JSON.parse(event.data);
            handleEvent(msg);
        };

        ws.onclose = function () {
            ws = null;
            stopMicStream();
            if (voiceAgentActive) setTimeout(connect, 2000);
        };

        ws.onerror = function () {
            showStatusTemp("接続エラー", "#f44", 3000);
        };
    }

    var watneyText = "";
    var pendingTranscript = null;
    var transcriptTimer = null;
    var pendingMute = false;
    var muteResponseId = null;
    var conversationItemIds = [];

    function deleteAllConversationItems() {
        for (var i = 0; i < conversationItemIds.length; i++) {
            ws.send(JSON.stringify({
                type: "conversation.item.delete",
                item_id: conversationItemIds[i]
            }));
        }
        conversationItemIds = [];
    }

    function handleEvent(event) {
        // 会話アイテムIDを追跡
        if (event.type === "conversation.item.created" && event.item && event.item.id) {
            conversationItemIds.push(event.item.id);
        }

        // response.createdでミュート対象のIDを確定
        if (event.type === "response.created" && pendingMute) {
            muteResponseId = event.response.id;
            pendingMute = false;
            return;
        }

        var isMuted = muteResponseId && event.response_id === muteResponseId;

        switch (event.type) {
            case "response.output_audio.delta":
                if (!isMuted) playAudioDelta(event.delta);
                break;
            case "response.audio_transcript.delta":
                watneyText += event.delta;
                break;
            case "response.audio_transcript.done":
                if (watneyText) addChatMessage("watney", watneyText);
                watneyText = "";
                break;
            case "response.done":
                // response.doneからワトニーの返答テキストを抽出
                if (!isMuted && event.response && event.response.output) {
                    for (var oi = 0; oi < event.response.output.length; oi++) {
                        var outputItem = event.response.output[oi];
                        if (outputItem.content) {
                            for (var ci = 0; ci < outputItem.content.length; ci++) {
                                var c = outputItem.content[ci];
                                if (c.transcript) {
                                    addChatMessage("watney", c.transcript);
                                } else if (c.text) {
                                    addChatMessage("watney", c.text);
                                }
                            }
                        }
                    }
                }
                if (isMuted) {
                    muteResponseId = null;
                    deleteAllConversationItems();
                }
                break;
            case "conversation.item.input_audio_transcription.completed":
                if (event.transcript) {
                    pendingTranscript = event.transcript;
                    if (transcriptTimer) clearTimeout(transcriptTimer);
                    transcriptTimer = setTimeout(function () {
                        if (pendingTranscript) addChatMessage("user", pendingTranscript);
                        pendingTranscript = null;
                    }, 500);
                }
                break;
            case "response.function_call_arguments.done":
                handleFunctionCall(event);
                break;
            case "error":
                console.error("Voice Agent error:", event.error);
                break;
        }
    }

    function handleFunctionCall(event) {
        var name = event.name;
        var args = {};
        try { args = JSON.parse(event.arguments); } catch(e) {}
        console.log("Function call:", name, args);

        if (name === "look") {
            // カメラVision
            var video = document.getElementById("vid");
            if (video && video.videoWidth > 0) {
                var canvas = document.createElement("canvas");
                canvas.width = 640; canvas.height = 480;
                canvas.getContext("2d").drawImage(video, 0, 0, 640, 480);
                var base64 = canvas.toDataURL("image/jpeg", 0.8).split(",")[1];
                $.ajax({
                    url: "/voiceAgentLook",
                    type: "POST",
                    data: JSON.stringify({ image: base64, question: args.question || "何が見える？" }),
                    contentType: "application/json",
                    dataType: "json"
                }).done(function (data) {
                    var desc = data.description || "よく見えない";
                    ws.send(JSON.stringify({
                        type: "conversation.item.create",
                        item: { type: "function_call_output", call_id: event.call_id, output: JSON.stringify({ description: desc }) }
                    }));
                    ws.send(JSON.stringify({ type: "response.create" }));
                });
            }
        } else {
            // サーバーにコマンド送信
            $.ajax({
                url: "/voiceAgentAction",
                type: "POST",
                data: JSON.stringify({ action: name, args: args }),
                contentType: "application/json",
                dataType: "json"
            });
            // ツール結果を返してターン完了
            ws.send(JSON.stringify({
                type: "conversation.item.create",
                item: { type: "function_call_output", call_id: event.call_id, output: JSON.stringify({ status: "ok" }) }
            }));
            // ターンを閉じる（クライアント側でミュート）
            pendingMute = true;
            ws.send(JSON.stringify({
                type: "response.create",
                response: { tools: [] }
            }));
        }
    }

    // --- Start/Stop ---
    async function startVoiceAgent() {
        try {
            var resp = await fetch("/getApiKey");
            var data = await resp.json();
            apiKey = data.key;

            mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
            audioContext = new AudioContext({ sampleRate: SAMPLE_RATE });

            var source = audioContext.createMediaStreamSource(mediaStream);
            audioProcessor = audioContext.createScriptProcessor(4096, 1, 1);
            source.connect(audioProcessor);
            audioProcessor.connect(audioContext.destination);
            audioProcessor.onaudioprocess = function (e) {
                audioBuffer.push(new Float32Array(e.inputBuffer.getChannelData(0)));
            };

            voiceAgentActive = true;
            updateButton();
            connect();
        } catch (err) {
            showStatusTemp("マイクの許可が必要です", "#f44", 3000);
        }
    }

    function stopVoiceAgent() {
        voiceAgentActive = false;
        stopMicStream();
        if (ws) { ws.close(); ws = null; }
        if (mediaStream) { mediaStream.getTracks().forEach(function (t) { t.stop(); }); mediaStream = null; }
        if (audioContext) { audioContext.close(); audioContext = null; }
        nextPlayTime = 0;
        updateButton();
        hideStatus();
        if (chatLog) chatLog.style.display = "none";
    }

    function toggleVoiceAgent() {
        if (voiceAgentActive) stopVoiceAgent();
        else startVoiceAgent();
    }

    // --- Key bindings ---
    document.addEventListener("keydown", function (e) {
        if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
        if (e.repeat) return;
        if (e.key === "g" || e.key === "G") {
            toggleVoiceAgent();
        } else if (e.key === " " && voiceAgentActive) {
            e.preventDefault();
            startTalking();
        }
    });

    document.addEventListener("keyup", function (e) {
        if (e.key === " " && voiceAgentActive) {
            e.preventDefault();
            stopTalking();
        }
    });

    window.toggleVoiceCommand = toggleVoiceAgent;
})();
