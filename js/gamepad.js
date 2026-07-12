// Gamepad controller support for Watney Rover
// Uses the standard Gamepad API
//
// PS4 DualShock 4 mapping:
//   Left stick: movement (8 directions)
//   Right stick up/down: camera look up/down
//   L1 (button 4): slow mode (hold)
//   X button (button 0): toggle headlights
//   Triangle (button 3): toggle AprilTag tracking

(function () {
    var gamepadIndex = null;
    var gamepadPollInterval = null;
    var gamepadLightsPressed = false;
    var gamepadTrackPressed = false;
    var gamepadPatrolPressed = false;
    var apriltagTracking = false;
    var apriltagPatrolling = false;
    var frameSendInterval = null;

    // Deadzone for analog sticks
    var STICK_DEADZONE = 0.3;

    // --- Gamepad Debug Overlay ---
    function ensureGamepadOverlay() {
        if (document.getElementById("gamepadOverlay")) return;
        var overlay = document.createElement("div");
        overlay.id = "gamepadOverlay";
        overlay.style.cssText = "position:fixed;bottom:10px;left:10px;z-index:9999;padding:8px 14px;border-radius:6px;background:rgba(0,0,0,0.75);color:#aaa;font-size:13px;font-family:monospace;pointer-events:none;";
        overlay.textContent = "Gamepad: not connected";
        document.body.appendChild(overlay);
    }

    function updateGamepadOverlay(text, color) {
        ensureGamepadOverlay();
        var el = document.getElementById("gamepadOverlay");
        el.textContent = text;
        el.style.color = color || "#aaa";
    }

    // Show initial state - try immediately and also after DOM load
    if (document.body) {
        ensureGamepadOverlay();
    } else {
        document.addEventListener("DOMContentLoaded", function () {
            ensureGamepadOverlay();
        });
    }

    window.addEventListener("gamepadconnected", function (e) {
        console.log("Gamepad connected: " + e.gamepad.id);
        gamepadIndex = e.gamepad.index;
        updateGamepadOverlay("Gamepad: " + e.gamepad.id, "#0f0");
        if (!gamepadPollInterval) {
            gamepadPollInterval = setInterval(pollGamepad, 50);
        }
    });

    window.addEventListener("gamepaddisconnected", function (e) {
        console.log("Gamepad disconnected: " + e.gamepad.id);
        updateGamepadOverlay("Gamepad: disconnected", "#f44");
        if (e.gamepad.index === gamepadIndex) {
            gamepadIndex = null;
            clearInterval(gamepadPollInterval);
            gamepadPollInterval = null;
            up = false;
            down = false;
            left = false;
            right = false;
            lookUp = false;
            lookDown = false;
            slow = false;
            sendKeys();
        }
    });

    // --- AprilTag Tracking UI ---

    function ensureTrackingUI() {
        if (document.getElementById("trackingOverlay")) return;
        var overlay = document.createElement("div");
        overlay.id = "trackingOverlay";
        overlay.style.cssText = "position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:9999;text-align:center;pointer-events:none;";
        overlay.innerHTML =
            '<div id="trackingBadge" style="display:none;padding:6px 16px;border-radius:6px;color:white;font-size:16px;font-weight:bold;margin-bottom:6px;"></div>' +
            '<div id="trackingInfo" style="display:none;padding:6px 16px;border-radius:6px;background:rgba(0,0,0,0.7);color:white;font-size:14px;"></div>';
        document.body.appendChild(overlay);
    }

    window.updateTrackingUI = updateTrackingUI;
    function updateTrackingUI(tracking, detection, patrol) {
        ensureTrackingUI();
        var badge = document.getElementById("trackingBadge");
        var info = document.getElementById("trackingInfo");

        if (patrol && patrol.completed) {
            // Patrol finished - all tags found
            apriltagPatrolling = false;
            badge.style.display = "block";
            badge.style.backgroundColor = "rgba(0,180,0,0.85)";
            badge.textContent = "Patrol COMPLETE! All " + patrol.route.length + " tags found";
            info.style.display = "block";
            info.innerHTML = "Route: " + patrol.route.join(" > ") + " ✔";
            stopFrameSending();
            if (!patrol._notified) {
                sendTTS("patrol complete");
                patrol._notified = true;
            }
        } else if (patrol && patrol.patrolling) {
            apriltagPatrolling = true;
            badge.style.display = "block";
            badge.style.backgroundColor = "rgba(0,100,220,0.85)";
            var stateLabel = {
                "searching": "Searching...",
                "scanning": "Scanning...",
                "approaching": "Approaching",
                "arrived": "Arrived!",
                "turning": "Turning..."
            };
            badge.textContent = "Patrol: " + (stateLabel[patrol.state] || patrol.state) +
                " [Target: " + patrol.targetId + "] (" + (patrol.routeIndex + 1) + "/" + patrol.route.length + ")";

            info.style.display = "block";
            var routeStr = "Route: " + patrol.route.map(function(id, i) {
                return i < patrol.routeIndex ? "✔" + id : (i === patrol.routeIndex ? "→" + id : id);
            }).join(" > ");
            if (patrol.detection) {
                info.innerHTML = routeStr +
                    " | ID: " + patrol.detection.id +
                    " | Size: " + (patrol.detection.area_ratio * 100).toFixed(1) + "%";
            } else {
                info.innerHTML = routeStr;
            }
        } else if (tracking) {
            apriltagPatrolling = false;
            badge.style.display = "block";
            badge.style.backgroundColor = "rgba(0,180,0,0.85)";
            badge.textContent = "AprilTag Tracking ON";

            if (detection) {
                info.style.display = "block";
                info.innerHTML = "ID: " + detection.id +
                    " | X: " + Math.round(detection.center_x) +
                    " Y: " + Math.round(detection.center_y) +
                    " | Size: " + (detection.area_ratio * 100).toFixed(1) + "%";
            } else {
                info.style.display = "block";
                info.innerHTML = "Searching...";
            }
        } else {
            apriltagPatrolling = false;
            badge.style.display = "none";
            info.style.display = "none";
        }
    }

    // --- Frame capture from video element ---

    function captureAndSendFrame() {
        var video = document.getElementById("vid");
        if (!video || video.videoWidth === 0) return;

        var canvas = document.createElement("canvas");
        canvas.width = 640;
        canvas.height = 480;
        var ctx = canvas.getContext("2d");
        ctx.drawImage(video, 0, 0, 640, 480);
        var dataUrl = canvas.toDataURL("image/jpeg", 0.7);
        var base64 = dataUrl.split(",")[1];

        $.ajax({
            url: '/apriltagFrame',
            type: "POST",
            data: JSON.stringify({ image: base64 }),
            contentType: "application/json; charset=utf-8"
        });
    }

    function startFrameSending() {
        if (frameSendInterval) return;
        frameSendInterval = setInterval(captureAndSendFrame, 500);
    }

    function stopFrameSending() {
        if (frameSendInterval) {
            clearInterval(frameSendInterval);
            frameSendInterval = null;
        }
    }

    // --- Patrol toggle ---

    function toggleAprilTagPatrol() {
        apriltagPatrolling = !apriltagPatrolling;
        $.ajax({
            url: '/apriltagPatrol',
            type: "POST",
            data: JSON.stringify({ enable: apriltagPatrolling, route: [3, 8, 11, 13] }),
            contentType: "application/json; charset=utf-8",
            dataType: "json"
        }).done(function (data) {
            apriltagPatrolling = data.patrolling;
            apriltagTracking = data.running;
            console.log("AprilTag patrol: " + (apriltagPatrolling ? "ON" : "OFF"));
            if (apriltagPatrolling) {
                startFrameSending();
                sendTTS("patrol start");
            } else {
                stopFrameSending();
                sendTTS("patrol stop");
            }
        }).fail(function () {
            apriltagPatrolling = false;
            console.log("AprilTag patrol: failed");
            stopFrameSending();
        });
    }

    // --- Tracking toggle ---

    function toggleAprilTagTracking() {
        apriltagTracking = !apriltagTracking;
        $.ajax({
            url: '/apriltagTrack',
            type: "POST",
            data: JSON.stringify({ enable: apriltagTracking }),
            contentType: "application/json; charset=utf-8",
            dataType: "json"
        }).done(function (data) {
            apriltagTracking = data.tracking;
            console.log("AprilTag tracking: " + (apriltagTracking ? "ON" : "OFF"));
            updateTrackingUI(apriltagTracking, null);
            sendTTS(apriltagTracking ? "tracking start" : "tracking stop");
            if (apriltagTracking) {
                startFrameSending();
            } else {
                stopFrameSending();
            }
        }).fail(function () {
            apriltagTracking = false;
            console.log("AprilTag tracking: failed");
            updateTrackingUI(false, null);
            stopFrameSending();
        });
    }

    // --- Gamepad polling ---

    function pollGamepad() {
        var gamepads = navigator.getGamepads();
        if (gamepadIndex === null) return;
        var gp = gamepads[gamepadIndex];
        if (!gp) return;

        // Debug: show button states
        var btnInfo = "Buttons: ";
        var pressed = [];
        for (var bi = 0; bi < gp.buttons.length; bi++) {
            if (gp.buttons[bi] && gp.buttons[bi].pressed) {
                pressed.push(bi);
            }
        }
        var axInfo = "L(" + (gp.axes[0]||0).toFixed(1) + "," + (gp.axes[1]||0).toFixed(1) + ") R(" + (gp.axes[2]||0).toFixed(1) + "," + (gp.axes[3]||0).toFixed(1) + ")";
        var trackStr = apriltagTracking ? " | Track:ON" : "";
        updateGamepadOverlay("GP: " + (pressed.length ? pressed.join(",") : "---") + " | " + axInfo + trackStr, "#0f0");

        // Left stick: axes[0] = X (left/right), axes[1] = Y (up/down)
        var lx = gp.axes[0] || 0;
        var ly = gp.axes[1] || 0;

        if (Math.abs(lx) < STICK_DEADZONE) lx = 0;
        if (Math.abs(ly) < STICK_DEADZONE) ly = 0;

        up = ly < -STICK_DEADZONE;
        down = ly > STICK_DEADZONE;
        left = lx < -STICK_DEADZONE;
        right = lx > STICK_DEADZONE;

        // Right stick Y: axes[3] = camera look
        var ry = gp.axes[3] || 0;
        if (Math.abs(ry) < STICK_DEADZONE) ry = 0;

        lookUp = ry < -STICK_DEADZONE;
        lookDown = ry > STICK_DEADZONE;

        // L1 (button 4): slow mode
        slow = gp.buttons[4] && gp.buttons[4].pressed;

        // X / Cross (button 0): toggle lights
        if (gp.buttons[0] && gp.buttons[0].pressed) {
            if (!gamepadLightsPressed) {
                gamepadLightsPressed = true;
                toggleLights();
            }
        } else {
            gamepadLightsPressed = false;
        }

        // Triangle (button 3): toggle AprilTag tracking
        if (gp.buttons[3] && gp.buttons[3].pressed) {
            if (!gamepadTrackPressed) {
                gamepadTrackPressed = true;
                toggleAprilTagTracking();
            }
        } else {
            gamepadTrackPressed = false;
        }

        // Circle (button 1): toggle AprilTag patrol
        if (gp.buttons[1] && gp.buttons[1].pressed) {
            if (!gamepadPatrolPressed) {
                gamepadPatrolPressed = true;
                toggleAprilTagPatrol();
            }
        } else {
            gamepadPatrolPressed = false;
        }

        sendKeys();
    }
})();
