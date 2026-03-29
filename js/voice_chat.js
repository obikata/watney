var voiceChatActive = false;
var voiceChatWs = null;
var voiceChatAudioContext = null;
var voiceChatMediaStream = null;
var voiceChatProcessor = null;
var voiceChatPlaybackTime = 0;
var voiceChatRecording = false;

var TARGET_SAMPLE_RATE = 24000;

function startVoiceChat() {
    if (voiceChatActive) {
        stopVoiceChat();
        return;
    }

    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
        voiceChatMediaStream = stream;
        voiceChatAudioContext = new (window.AudioContext || window.webkitAudioContext)();
        voiceChatPlaybackTime = 0;

        var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        voiceChatWs = new WebSocket(protocol + '//' + window.location.host + '/voiceChat');

        voiceChatWs.onopen = function () {
            voiceChatActive = true;
            syncVoiceChatUI();

            var source = voiceChatAudioContext.createMediaStreamSource(stream);
            voiceChatProcessor = voiceChatAudioContext.createScriptProcessor(4096, 1, 1);

            voiceChatProcessor.onaudioprocess = function (e) {
                if (!voiceChatActive || !voiceChatRecording || !voiceChatWs || voiceChatWs.readyState !== WebSocket.OPEN) return;

                var inputData = e.inputBuffer.getChannelData(0);
                var resampled = resampleAudio(inputData, voiceChatAudioContext.sampleRate, TARGET_SAMPLE_RATE);
                var pcm16 = float32ToPcm16(resampled);
                var base64 = arrayBufferToBase64(pcm16.buffer);

                voiceChatWs.send(JSON.stringify({
                    type: 'input_audio_buffer.append',
                    audio: base64
                }));
            };

            source.connect(voiceChatProcessor);
            voiceChatProcessor.connect(voiceChatAudioContext.destination);
        };

        voiceChatWs.onmessage = function (event) {
            var data = JSON.parse(event.data);

            if (data.type === 'response.created') {
                voiceChatPlaybackTime = 0;
            } else if (data.type === 'response.output_audio.delta') {
                playAudioChunk(data.delta);
            } else if (data.type === 'response.output_audio_transcript.delta') {
                appendTranscript(data.delta, 'ai');
            } else if (data.type === 'response.output_audio_transcript.done') {
                // Final transcript received
            } else if (data.type === 'conversation.item.input_audio_transcription.completed') {
                setTranscript(data.transcript, 'user');
            } else if (data.type === 'error') {
                console.error('Voice chat error:', data.error);
                appendTranscript('[Error: ' + (data.error.message || 'Unknown') + ']', 'system');
            }
        };

        voiceChatWs.onclose = function () {
            stopVoiceChat();
        };

        voiceChatWs.onerror = function () {
            stopVoiceChat();
        };

    }).catch(function (err) {
        console.error('Microphone access denied:', err);
        alert('Microphone access is required for voice chat.');
    });
}

function stopVoiceChat() {
    voiceChatActive = false;
    voiceChatRecording = false;

    if (voiceChatProcessor) {
        voiceChatProcessor.disconnect();
        voiceChatProcessor = null;
    }
    if (voiceChatMediaStream) {
        voiceChatMediaStream.getTracks().forEach(function (t) { t.stop(); });
        voiceChatMediaStream = null;
    }
    if (voiceChatWs && voiceChatWs.readyState === WebSocket.OPEN) {
        voiceChatWs.close();
    }
    voiceChatWs = null;
    if (voiceChatAudioContext) {
        voiceChatAudioContext.close();
        voiceChatAudioContext = null;
    }
    voiceChatPlaybackTime = 0;

    syncVoiceChatUI();
}

function voiceChatStartRecording() {
    if (!voiceChatActive || voiceChatRecording) return;
    voiceChatRecording = true;
    if (voiceChatWs && voiceChatWs.readyState === WebSocket.OPEN) {
        voiceChatWs.send(JSON.stringify({type: 'input_audio_buffer.clear'}));
    }
    $("#voiceChatButton").addClass("recording");
}

function voiceChatStopRecording() {
    if (!voiceChatActive || !voiceChatRecording) return;
    voiceChatRecording = false;
    $("#voiceChatButton").removeClass("recording");
    if (voiceChatWs && voiceChatWs.readyState === WebSocket.OPEN) {
        voiceChatWs.send(JSON.stringify({type: 'input_audio_buffer.commit'}));
        voiceChatWs.send(JSON.stringify({type: 'response.create'}));
    }
}

function playAudioChunk(base64Audio) {
    if (!voiceChatAudioContext) return;

    var pcm16 = base64ToInt16Array(base64Audio);
    var float32 = pcm16ToFloat32(pcm16);

    var audioBuffer = voiceChatAudioContext.createBuffer(1, float32.length, TARGET_SAMPLE_RATE);
    audioBuffer.getChannelData(0).set(float32);

    var source = voiceChatAudioContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(voiceChatAudioContext.destination);

    var now = voiceChatAudioContext.currentTime;
    if (voiceChatPlaybackTime < now) {
        voiceChatPlaybackTime = now;
    }
    source.start(voiceChatPlaybackTime);
    voiceChatPlaybackTime += audioBuffer.duration;
}

// --- Audio conversion utilities ---

function resampleAudio(inputData, fromRate, toRate) {
    if (fromRate === toRate) return inputData;
    var ratio = fromRate / toRate;
    var newLength = Math.round(inputData.length / ratio);
    var result = new Float32Array(newLength);
    for (var i = 0; i < newLength; i++) {
        var index = i * ratio;
        var low = Math.floor(index);
        var high = Math.min(low + 1, inputData.length - 1);
        var frac = index - low;
        result[i] = inputData[low] * (1 - frac) + inputData[high] * frac;
    }
    return result;
}

function float32ToPcm16(float32) {
    var pcm16 = new Int16Array(float32.length);
    for (var i = 0; i < float32.length; i++) {
        var s = Math.max(-1, Math.min(1, float32[i]));
        pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return pcm16;
}

function pcm16ToFloat32(pcm16) {
    var float32 = new Float32Array(pcm16.length);
    for (var i = 0; i < pcm16.length; i++) {
        float32[i] = pcm16[i] / (pcm16[i] < 0 ? 0x8000 : 0x7FFF);
    }
    return float32;
}

function arrayBufferToBase64(buffer) {
    var bytes = new Uint8Array(buffer);
    var binary = '';
    for (var i = 0; i < bytes.byteLength; i++) {
        binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
}

function base64ToInt16Array(base64) {
    var binary = atob(base64);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
    }
    return new Int16Array(bytes.buffer);
}

// --- UI ---

function syncVoiceChatUI() {
    if (voiceChatActive) {
        $("div#voiceChatButton > i").text("voice_over_off");
        $("div#voiceChatButton").addClass("active");
        $("#voiceChatSection").fadeIn(200);
    } else {
        $("div#voiceChatButton > i").text("record_voice_over");
        $("div#voiceChatButton").removeClass("active");
        $("#voiceChatSection").fadeOut(200);
    }
}

function appendTranscript(text, role) {
    var container = $("#voiceChatTranscript");
    var lastEntry = container.find("." + role + "-msg:last");
    if (lastEntry.length && role === 'ai') {
        lastEntry.append(text);
    } else {
        var prefix = role === 'user' ? 'You: ' : role === 'ai' ? 'Watney: ' : '';
        container.append('<div class="' + role + '-msg">' + prefix + text + '</div>');
    }
    container.scrollTop(container[0].scrollHeight);
}

function setTranscript(text, role) {
    var container = $("#voiceChatTranscript");
    var prefix = role === 'user' ? 'You: ' : role === 'ai' ? 'Watney: ' : '';
    container.append('<div class="' + role + '-msg">' + prefix + text + '</div>');
    container.scrollTop(container[0].scrollHeight);
}
