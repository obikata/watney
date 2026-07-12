import cv2
from apriltag import apriltag
import numpy as np
import base64
import time
import sys
import threading


class AprilTagTracker:
    # States for patrol mode
    STATE_IDLE = "idle"
    STATE_APPROACHING = "approaching"
    STATE_ARRIVED = "arrived"
    STATE_TURNING = "turning"
    STATE_SCANNING = "scanning"

    def __init__(self, config, motorController):
        self.motorController = motorController
        self.running = False

        atConfig = config["APRILTAG"]
        self.tagFamily = atConfig.get("TagFamily", "tag36h11")
        self.width = int(atConfig.get("Width", "640"))
        self.height = int(atConfig.get("Height", "480"))
        self.stopThreshold = float(atConfig.get("StopThreshold", "0.017"))
        self.slowApproach = atConfig.getboolean("SlowApproach", True)
        self.searchTimeout = float(atConfig.get("SearchTimeoutS", "30"))
        self.arrivedPause = float(atConfig.get("ArrivedPauseS", "2.0"))
        self.searchTurnSpeed = int(atConfig.get("SearchTurnSpeed", "28"))
        self.turnStepS = float(atConfig.get("TurnStepS", "0.5"))
        self.scanPauseS = float(atConfig.get("ScanPauseS", "1.5"))

        self.detector = apriltag(self.tagFamily)
        self.lastDetection = None
        self._manualOverrideUntil = 0

        # Patrol mode
        self.patrolRoute = []
        self.patrolIndex = 0
        self.patrolState = self.STATE_IDLE
        self._stateStartTime = 0
        self._searchStartTime = 0
        self._targetId = None

        # Background search thread
        self._searchThread = None
        self._searchStop = threading.Event()
        self._lostCount = 0
        self._lostThreshold = 3  # frames before declaring tag lost

    def start(self):
        if self.running:
            return
        self.running = True
        self.lastDetection = None
        self.patrolRoute = []
        self.patrolState = self.STATE_IDLE
        self._targetId = None
        print("AprilTag tracker started")

    def startPatrol(self, route):
        if not route:
            return
        self.running = True
        self.patrolRoute = route
        self.patrolIndex = 0
        self._setTarget(self.patrolRoute[0])
        print(f"Patrol started: route={route}")

    def _setTarget(self, tagId):
        self._targetId = tagId
        self._searchStartTime = time.time()
        self.lastDetection = None
        self._startSearchCycle()
        print(f"Patrol: seeking tag {tagId}")

    def _advancePatrol(self):
        self.patrolIndex += 1
        if self.patrolIndex >= len(self.patrolRoute):
            # All tags found - patrol complete
            print(f"Patrol: COMPLETE! All {len(self.patrolRoute)} tags found")
            self.patrolState = self.STATE_IDLE
            self._targetId = None
            self.running = False
            return
        nextId = self.patrolRoute[self.patrolIndex]
        self._targetId = nextId
        self._searchStartTime = time.time()
        self._startSearchCycle()
        print(f"Patrol: turning to find tag {nextId}")

    def _startSearchCycle(self):
        """Start the background turn/scan cycle thread."""
        self._stopSearchCycle()
        self._searchStop.clear()
        self._searchThread = threading.Thread(target=self._searchCycleLoop, daemon=True)
        self._searchThread.start()

    def _stopSearchCycle(self):
        """Stop the background turn/scan cycle thread."""
        self._searchStop.set()
        if self._searchThread is not None:
            self._searchThread.join(timeout=3)
            self._searchThread = None

    def _searchCycleLoop(self):
        """Background thread: alternates between turning and scanning."""
        try:
            while not self._searchStop.is_set():
                # TURNING phase
                self.patrolState = self.STATE_TURNING
                self._stateStartTime = time.time()
                speed = self.searchTurnSpeed
                self.motorController.setMotorDC(speed, -speed)
                print(f"Patrol: ROTATE START speed={speed}", flush=True)

                if self._searchStop.wait(self.turnStepS):
                    print("Patrol: search cycle stopped during turn", flush=True)
                    return

                # SCANNING phase: STOP motors
                self.motorController.setMotorDC(0, 0)
                self.patrolState = self.STATE_SCANNING
                self._stateStartTime = time.time()
                print(f"Patrol: MOTORS STOPPED, scanning for {self.scanPauseS}s", flush=True)

                if self._searchStop.wait(self.scanPauseS):
                    print("Patrol: search cycle stopped during scan", flush=True)
                    return
        except Exception as e:
            print(f"Patrol: search cycle CRASHED: {e}", flush=True)
            import traceback
            traceback.print_exc()

    def stop(self):
        if not self.running:
            return
        self.running = False
        self._stopSearchCycle()
        self.motorController.setBearing("0", False)
        self.lastDetection = None
        self.patrolRoute = []
        self.patrolState = self.STATE_IDLE
        self._targetId = None
        print("AprilTag tracker stopped")

    def isRunning(self):
        return self.running

    def isPatrolling(self):
        return self.running and len(self.patrolRoute) > 0

    def getStatus(self):
        completed = (not self.running and len(self.patrolRoute) > 0
                     and self.patrolIndex >= len(self.patrolRoute))
        return {
            "running": self.running,
            "patrolling": self.isPatrolling(),
            "completed": completed,
            "state": self.patrolState,
            "targetId": self._targetId,
            "route": self.patrolRoute,
            "routeIndex": self.patrolIndex,
            "detection": self.lastDetection,
        }

    def getLastDetection(self):
        return self.lastDetection

    def onManualCommand(self):
        self._manualOverrideUntil = time.time() + 1.5

    def _isManualOverride(self):
        return time.time() < self._manualOverrideUntil

    def processFrame(self, imageBase64):
        if not self.running:
            return

        imgData = base64.b64decode(imageBase64)
        imgArray = np.frombuffer(imgData, dtype=np.uint8)
        frame = cv2.imdecode(imgArray, cv2.IMREAD_COLOR)
        if frame is None:
            return

        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        detections = self.detector.detect(gray)

        det_ids = [d['id'] for d in detections] if detections else []
        print(f"Frame: {w}x{h} tags={det_ids} target={self._targetId} state={self.patrolState}", flush=True)

        if self.isPatrolling():
            self._processPatrolFrame(detections, w, h)
        else:
            self._processTrackFrame(detections, w, h)

    def _processTrackFrame(self, detections, w, h):
        if detections:
            best = max(detections, key=lambda d: self._tagArea(d))
            self._approachTag(best, w, h)
        else:
            if not self._isManualOverride():
                self.motorController.setBearing("0", False)
            self.lastDetection = None

    def _processPatrolFrame(self, detections, w, h):
        now = time.time()

        if self._isManualOverride():
            target = self._findTarget(detections)
            if target:
                self._updateDetectionInfo(target, w, h)
            return

        # While searching (TURNING or SCANNING), check if we found the target
        if self.patrolState in (self.STATE_TURNING, self.STATE_SCANNING):
            target = self._findTarget(detections)
            if target:
                # Found it! Stop search cycle, approach
                self._stopSearchCycle()
                self.motorController.setBearing("0", False)
                self.patrolState = self.STATE_APPROACHING
                self._stateStartTime = now
                # Wait briefly for rover to settle before approaching
                time.sleep(0.3)
                self._approachTag(target, w, h)
                print(f"Patrol: found tag {self._targetId}, approaching")
                return
            # During TURNING phase, don't do anything - frames are blurry
            if self.patrolState == self.STATE_TURNING:
                return

            # Overall search timeout
            totalSearchTime = now - self._searchStartTime
            if totalSearchTime > self.searchTimeout:
                self._stopSearchCycle()
                self._searchStartTime = now
                self._startSearchCycle()
                print(f"Patrol: search timeout for tag {self._targetId}, retrying")

        elif self.patrolState == self.STATE_APPROACHING:
            target = self._findTarget(detections)
            if target:
                self._lostCount = 0
                tagArea = self._tagArea(target)
                areaRatio = tagArea / (w * h)
                self._updateDetectionInfo(target, w, h)
                if areaRatio >= self.stopThreshold:
                    self.motorController.setBearing("0", False)
                    if self.patrolState != self.STATE_ARRIVED:
                        self.patrolState = self.STATE_ARRIVED
                        self._stateStartTime = now
                        print(f"Patrol: arrived at tag {self._targetId}")
                else:
                    self._approachTag(target, w, h)
            else:
                self._lostCount += 1
                print(f"Patrol: tag {self._targetId} not detected ({self._lostCount}/{self._lostThreshold})")
                if self._lostCount >= self._lostThreshold:
                    # Really lost the tag
                    self._lostCount = 0
                    self.motorController.setBearing("0", False)
                    self.patrolState = self.STATE_TURNING
                    self._searchStartTime = now
                    self.lastDetection = None
                    self._startSearchCycle()
                    print(f"Patrol: lost tag {self._targetId}, searching")

        elif self.patrolState == self.STATE_ARRIVED:
            elapsed = now - self._stateStartTime
            if elapsed >= self.arrivedPause:
                self._advancePatrol()

    def _findTarget(self, detections):
        if not detections:
            return None
        for d in detections:
            if d['id'] == self._targetId:
                return d
        return None

    def _updateDetectionInfo(self, detection, frameW, frameH):
        tagArea = self._tagArea(detection)
        areaRatio = tagArea / (frameW * frameH)
        self.lastDetection = {
            "id": int(detection['id']),
            "center_x": round(detection['center'][0], 1),
            "center_y": round(detection['center'][1], 1),
            "area_ratio": round(areaRatio, 4),
        }

    def _tagArea(self, detection):
        corners = detection['lb-rb-rt-lt']
        n = len(corners)
        area = 0
        for i in range(n):
            j = (i + 1) % n
            area += corners[i][0] * corners[j][1]
            area -= corners[j][0] * corners[i][1]
        return abs(area) / 2.0

    def _approachTag(self, detection, frameW, frameH):
        frameArea = frameW * frameH
        tagArea = self._tagArea(detection)
        areaRatio = tagArea / frameArea
        tagId = detection['id']
        center = detection['center']

        self._updateDetectionInfo(detection, frameW, frameH)

        if self._isManualOverride():
            print(f"AprilTag {tagId}: detected (manual override active)")
            return

        if areaRatio >= self.stopThreshold:
            self.motorController.setBearing("0", False)
            print(f"AprilTag {tagId}: reached (area={areaRatio:.3f})")
            return

        centerX = center[0]
        offset = (centerX - frameW / 2) / (frameW / 2)

        if offset < -0.3:
            bearing = "nw"
        elif offset > 0.3:
            bearing = "ne"
        else:
            bearing = "n"

        self.motorController.setBearing(bearing, self.slowApproach)
        print(f"AprilTag {tagId}: bearing={bearing} offset={offset:.2f} area={areaRatio:.3f}")
