// Voice Command - ブラウザのマイクで録音 → xAI STT → コマンド実行
(function () {
    var mediaRecorder = null;
    var audioChunks = [];
    var voiceCommandActive = false;
    var recording = false;
    var voiceOverlay = null;
    var stream = null;

    function ensureOverlay() {
        if (voiceOverlay) return;
        voiceOverlay = document.createElement("div");
        voiceOverlay.id = "voiceCommandOverlay";
        voiceOverlay.style.cssText = "position:fixed;bottom:60px;left:50%;transform:translateX(-50%);z-index:9999;padding:10px 20px;border-radius:8px;background:rgba(0,0,0,0.8);color:white;font-size:16px;font-family:sans-serif;text-align:center;pointer-events:none;display:none;";
        document.body.appendChild(voiceOverlay);
    }

    function showVoiceStatus(text, color) {
        ensureOverlay();
        voiceOverlay.textContent = text;
        voiceOverlay.style.color = color || "white";
        voiceOverlay.style.display = "block";
    }

    function hideVoiceStatus() {
        if (voiceOverlay) voiceOverlay.style.display = "none";
    }

    function showVoiceStatusTemp(text, color, duration) {
        showVoiceStatus(text, color);
        setTimeout(hideVoiceStatus, duration || 2000);
    }

    function updateVoiceButton() {
        var btn = document.getElementById("voiceCommandButton");
        if (!btn) return;
        var icon = btn.querySelector("i");
        if (voiceCommandActive) {
            icon.textContent = "hearing";
            btn.style.color = "#0f0";
        } else {
            icon.textContent = "hearing_disabled";
            btn.style.color = "";
        }
    }

    function toggleVoiceCommand() {
        if (voiceCommandActive) {
            stopVoiceCommand();
        } else {
            startVoiceCommand();
        }
    }

    function startVoiceCommand() {
        navigator.mediaDevices.getUserMedia({ audio: true })
            .then(function (s) {
                stream = s;
                voiceCommandActive = true;
                updateVoiceButton();
                showVoiceStatusTemp("音声コマンド ON (Mキー長押しで録音)", "#0f0", 3000);
            })
            .catch(function (err) {
                console.error("Mic error:", err);
                showVoiceStatusTemp("マイクの許可が必要です", "#f44", 3000);
            });
    }

    function stopVoiceCommand() {
        voiceCommandActive = false;
        if (stream) {
            stream.getTracks().forEach(function (t) { t.stop(); });
            stream = null;
        }
        updateVoiceButton();
        showVoiceStatusTemp("音声コマンド OFF", "#aaa", 2000);
    }

    function startRecording() {
        if (!voiceCommandActive || !stream || recording) return;
        audioChunks = [];
        mediaRecorder = new MediaRecorder(stream, { mimeType: "audio/webm" });
        mediaRecorder.ondataavailable = function (e) {
            if (e.data.size > 0) audioChunks.push(e.data);
        };
        mediaRecorder.onstop = function () {
            var blob = new Blob(audioChunks, { type: "audio/webm" });
            sendAudioToServer(blob);
        };
        mediaRecorder.start();
        recording = true;
        showVoiceStatus("🎤 録音中...", "#f44");
    }

    function stopRecording() {
        if (!recording || !mediaRecorder) return;
        mediaRecorder.stop();
        recording = false;
        showVoiceStatus("認識中...", "#ff0");
    }

    function sendAudioToServer(blob) {
        var formData = new FormData();
        formData.append("audio", blob, "audio.webm");

        $.ajax({
            url: "/voiceCommand",
            type: "POST",
            data: formData,
            processData: false,
            contentType: false
        }).done(function (data) {
            if (data.transcript) {
                var actionLabel = data.action ? " → " + data.action : " (不明)";
                var color = data.action ? "#0f0" : "#ff0";
                showVoiceStatusTemp("「" + data.transcript + "」" + actionLabel, color, 3000);
            } else {
                showVoiceStatusTemp("聞き取れませんでした", "#f80", 2000);
            }
        }).fail(function () {
            showVoiceStatusTemp("エラーが発生しました", "#f44", 2000);
        });
    }

    // Mキー: 長押しで録音
    document.addEventListener("keydown", function (e) {
        if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
        if (e.repeat) return;
        if (e.key === "m" || e.key === "M") {
            if (!voiceCommandActive) {
                toggleVoiceCommand();
            } else {
                startRecording();
            }
        }
    });

    document.addEventListener("keyup", function (e) {
        if (e.key === "m" || e.key === "M") {
            if (recording) {
                stopRecording();
            }
        }
    });

    // --- TTS再生（サーバーからの音声をブラウザで再生） ---
    var ttsPollInterval = null;
    var ttsPlaying = false;

    function startTTSPoll() {
        if (ttsPollInterval) return;
        ttsPollInterval = setInterval(pollTTS, 1000);
    }

    function pollTTS() {
        if (ttsPlaying) return;
        $.getJSON("/ttsPending", function (data) {
            if (data.texts && data.texts.length > 0) {
                playTTSQueue(data.texts, 0);
            }
        });
    }

    function playTTSQueue(texts, index) {
        if (index >= texts.length) {
            ttsPlaying = false;
            return;
        }
        ttsPlaying = true;
        $.ajax({
            url: "/ttsSpeak",
            type: "POST",
            data: JSON.stringify({ text: texts[index] }),
            contentType: "application/json",
            xhrFields: { responseType: "blob" }
        }).done(function (blob) {
            var url = URL.createObjectURL(blob);
            var audio = new Audio(url);
            audio.onended = function () {
                URL.revokeObjectURL(url);
                playTTSQueue(texts, index + 1);
            };
            audio.onerror = function () {
                URL.revokeObjectURL(url);
                playTTSQueue(texts, index + 1);
            };
            audio.play();
        }).fail(function () {
            playTTSQueue(texts, index + 1);
        });
    }

    // ページ読み込み時にTTSポーリング開始
    startTTSPoll();

    // グローバルに公開
    window.toggleVoiceCommand = toggleVoiceCommand;
})();
