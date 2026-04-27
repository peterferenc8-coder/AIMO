#!/usr/bin/env python3
"""
ESP32-C3 OSSM + StrokeEngine Device Emulator

Faithful Python port of the StrokeEngine state machine and all 7 patterns.
Replicates the C++ firmware behaviour over stdio/serial JSON.
"""

import asyncio
import json
import math
import sys
import time
import threading
import queue
from typing import Optional

try:
    import serial
except ImportError:
    serial = None


# =============================================================
#  CONFIGURATION
# =============================================================

STEPS_PER_REV = 200
MICROSTEPPING = 8
TOTAL_STEPS_PER_REV = STEPS_PER_REV * MICROSTEPPING   # 1600

DEFAULT_MM_PER_STEP = 40.0 / TOTAL_STEPS_PER_REV

STREAM_ACCEL_HZ = 80000
STREAM_MAX_SPEED_HZ = 20000
JOG_SPEED_HZ = 800
JOG_ACCEL_HZ = 1600

MAX_SPEED_MM_S = 300.0
MIN_PATTERN_SPEED_SPM = 6.0
MAX_PATTERN_SPEED_SPM = 300.0

MANUAL_SPEED_MIN_HZ = 300
MANUAL_SPEED_MAX_HZ = 6000
MANUAL_ACCEL_MIN_HZ = 600
MANUAL_ACCEL_MAX_HZ = 18000

CALIB_MOVE_SIGN = -1

PHYSICS_HZ = 100
PHYSICS_DT = 1.0 / PHYSICS_HZ


# =============================================================
#  HELPERS
# =============================================================

def constrain(val, min_val, max_val):
    return max(min_val, min(val, max_val))


# =============================================================
#  PATTERN MATH  (ported from PatternMath.h)
# =============================================================

def fscale(originalMin, originalMax, newBegin, newEnd, inputValue, curve):
    if curve > 10:
        curve = 10
    if curve < -10:
        curve = -10
    curve = (curve * -0.1)
    curve = math.pow(10, curve)

    if inputValue < originalMin:
        inputValue = originalMin
    if inputValue > originalMax:
        inputValue = originalMax

    OriginalRange = originalMax - originalMin
    invFlag = False
    if newEnd > newBegin:
        NewRange = newEnd - newBegin
    else:
        NewRange = newBegin - newEnd
        invFlag = True

    zeroRefCurVal = inputValue - originalMin
    normalizedCurVal = zeroRefCurVal / OriginalRange

    if originalMin > originalMax:
        return 0

    if not invFlag:
        rangedValue = (math.pow(normalizedCurVal, curve) * NewRange) + newBegin
    else:
        rangedValue = newBegin - (math.pow(normalizedCurVal, curve) * NewRange)
    return rangedValue


def fmap(x, in_min, in_max, out_min, out_max):
    return (x - in_min) * (out_max - out_min) / (in_max - in_min) + out_min


def mapSensationToFactor(maximumFactor, inputValue, curve=0.0):
    inputValue = constrain(inputValue, -100.0, 100.0)
    if inputValue == 0.0:
        return 1.0
    fscaledValue = fscale(0.0, 100.0, 1.0, maximumFactor, abs(inputValue), curve)
    if inputValue >= 0:
        return fscaledValue
    else:
        return 1.0 / fscaledValue


# =============================================================
#  MOTION PARAMETER  (ported from pattern.h)
# =============================================================

class motionParameter:
    def __init__(self, stroke=0, speed=0, acceleration=0, skip=False):
        self.stroke = stroke
        self.speed = speed
        self.acceleration = acceleration
        self.skip = skip


# =============================================================
#  PATTERNS  (ported from pattern.h)
# =============================================================

class Pattern:
    def __init__(self, name):
        self._name = name
        self._stroke = 0
        self._depth = 0
        self._timeOfStroke = 1.0
        self._sensation = 0.0
        self._index = -1
        self._maxSpeed = 0
        self._maxAcceleration = 0
        self._stepsPerMM = 0
        self._nextMove = motionParameter()
        self._startDelayMillis = 0
        self._delayInMillis = 0

    def setTimeOfStroke(self, speed):
        self._timeOfStroke = speed

    def setStroke(self, stroke):
        self._stroke = stroke

    def setDepth(self, depth):
        self._depth = depth

    def setSensation(self, sensation):
        self._sensation = sensation

    def getName(self):
        return self._name

    def nextTarget(self, index):
        self._index = index
        return self._nextMove

    def setSpeedLimit(self, maxSpeed, maxAcceleration, stepsPerMM):
        self._maxSpeed = maxSpeed
        self._maxAcceleration = maxAcceleration
        self._stepsPerMM = stepsPerMM

    def _startDelay(self):
        self._startDelayMillis = int(time.time() * 1000)

    def _updateDelay(self, delayInMillis):
        self._delayInMillis = delayInMillis

    def _isStillDelayed(self):
        return int(time.time() * 1000) < (self._startDelayMillis + self._delayInMillis)


class SimpleStroke(Pattern):
    def __init__(self):
        super().__init__("Simple Stroke")

    def setTimeOfStroke(self, speed=0):
        self._timeOfStroke = 0.5 * speed

    def nextTarget(self, index):
        self._nextMove.speed = int(1.5 * self._stroke / self._timeOfStroke)
        self._nextMove.acceleration = int(3.0 * self._nextMove.speed / self._timeOfStroke)
        if index % 2:
            self._nextMove.stroke = self._depth - self._stroke
        else:
            self._nextMove.stroke = self._depth
        self._index = index
        return self._nextMove


class TeasingPounding(Pattern):
    def __init__(self):
        super().__init__("Teasing or Pounding")
        self._timeOfFastStroke = 1.0
        self._timeOfInStroke = 1.0
        self._timeOfOutStroke = 1.0

    def setSensation(self, sensation):
        self._sensation = sensation
        self._updateStrokeTiming()

    def setTimeOfStroke(self, speed=0):
        self._timeOfStroke = speed
        self._updateStrokeTiming()

    def _updateStrokeTiming(self):
        self._timeOfFastStroke = (0.5 * self._timeOfStroke) / fscale(0.0, 100.0, 1.0, 5.0, abs(self._sensation), 0.0)
        if self._sensation > 0.0:
            self._timeOfInStroke = self._timeOfFastStroke
            self._timeOfOutStroke = self._timeOfStroke - self._timeOfFastStroke
        else:
            self._timeOfOutStroke = self._timeOfFastStroke
            self._timeOfInStroke = self._timeOfStroke - self._timeOfFastStroke

    def nextTarget(self, index):
        if index % 2:
            self._nextMove.speed = int(1.5 * self._stroke / self._timeOfOutStroke)
            self._nextMove.acceleration = int(3.0 * float(self._nextMove.speed) / self._timeOfOutStroke)
            self._nextMove.stroke = self._depth - self._stroke
        else:
            self._nextMove.speed = int(1.5 * self._stroke / self._timeOfInStroke)
            self._nextMove.acceleration = int(3.0 * float(self._nextMove.speed) / self._timeOfInStroke)
            self._nextMove.stroke = self._depth
        self._index = index
        return self._nextMove


class RoboStroke(Pattern):
    def __init__(self):
        super().__init__("Robo Stroke")
        self._x = 1.0 / 3.0

    def setTimeOfStroke(self, speed=0):
        self._timeOfStroke = 0.5 * speed

    def setSensation(self, sensation=0):
        self._sensation = sensation
        if sensation >= 0:
            self._x = fscale(0.0, 100.0, 1.0 / 3.0, 0.5, sensation, 0.0)
        else:
            self._x = fscale(0.0, 100.0, 1.0 / 3.0, 0.05, -sensation, 0.0)

    def nextTarget(self, index):
        speed = float(self._stroke) / ((1.0 - self._x) * self._timeOfStroke)
        self._nextMove.speed = int(speed)
        self._nextMove.acceleration = int(speed / (self._x * self._timeOfStroke))
        if index % 2:
            self._nextMove.stroke = self._depth - self._stroke
        else:
            self._nextMove.stroke = self._depth
        self._index = index
        return self._nextMove


class HalfnHalf(Pattern):
    def __init__(self):
        super().__init__("Half'n'Half")
        self._timeOfFastStroke = 1.0
        self._timeOfInStroke = 1.0
        self._timeOfOutStroke = 1.0
        self._half = True

    def setSensation(self, sensation):
        self._sensation = sensation
        self._updateStrokeTiming()

    def setTimeOfStroke(self, speed=0):
        self._timeOfStroke = speed
        self._updateStrokeTiming()

    def _updateStrokeTiming(self):
        self._timeOfFastStroke = (0.5 * self._timeOfStroke) / fscale(0.0, 100.0, 1.0, 5.0, abs(self._sensation), 0.0)
        if self._sensation > 0.0:
            self._timeOfInStroke = self._timeOfFastStroke
            self._timeOfOutStroke = self._timeOfStroke - self._timeOfFastStroke
        else:
            self._timeOfOutStroke = self._timeOfFastStroke
            self._timeOfInStroke = self._timeOfStroke - self._timeOfFastStroke

    def nextTarget(self, index):
        if index == 0:
            self._half = True
        stroke = self._stroke
        if self._half:
            stroke = self._stroke // 2
        if index % 2:
            self._nextMove.speed = int(1.5 * stroke / self._timeOfOutStroke)
            self._nextMove.acceleration = int(3.0 * float(self._nextMove.speed) / self._timeOfOutStroke)
            self._nextMove.stroke = self._depth - self._stroke
            self._half = not self._half
        else:
            self._nextMove.speed = int(1.5 * stroke / self._timeOfInStroke)
            self._nextMove.acceleration = int(3.0 * float(self._nextMove.speed) / self._timeOfInStroke)
            self._nextMove.stroke = (self._depth - self._stroke) + stroke
        self._index = index
        return self._nextMove


class Deeper(Pattern):
    def __init__(self):
        super().__init__("Deeper")
        self._countStrokesForRamp = 2

    def setTimeOfStroke(self, speed=0):
        self._timeOfStroke = 0.5 * speed

    def setSensation(self, sensation):
        self._sensation = sensation
        if sensation < 0:
            self._countStrokesForRamp = int(fmap(sensation, -100, 0, 2, 11))
        else:
            self._countStrokesForRamp = int(fmap(sensation, 0, 100, 11, 32))

    def nextTarget(self, index):
        slope = self._stroke // self._countStrokesForRamp
        cycleIndex = (index // 2) % self._countStrokesForRamp + 1
        amplitude = slope * cycleIndex
        self._nextMove.speed = int(1.5 * amplitude / self._timeOfStroke)
        self._nextMove.acceleration = int(3.0 * self._nextMove.speed / self._timeOfStroke)
        if index % 2:
            self._nextMove.stroke = self._depth - self._stroke
        else:
            self._nextMove.stroke = (self._depth - self._stroke) + amplitude
        self._index = index
        return self._nextMove


class StopNGo(Pattern):
    def __init__(self):
        super().__init__("Stop'n'Go")
        self._numberOfStrokes = 5
        self._strokeSeriesIndex = 1
        self._strokeIndex = 0
        self._countStrokesUp = True

    def setTimeOfStroke(self, speed=0):
        self._timeOfStroke = 0.5 * speed

    def setSensation(self, sensation):
        self._sensation = sensation
        self._updateDelay(int(fmap(sensation, -100, 100, 100, 10000)))

    def nextTarget(self, index):
        self._nextMove.speed = int(1.5 * self._stroke / self._timeOfStroke)
        self._nextMove.acceleration = int(3.0 * self._nextMove.speed / self._timeOfStroke)
        if not self._isStillDelayed():
            if index % 2:
                self._nextMove.stroke = self._depth - self._stroke
                if self._strokeIndex >= self._strokeSeriesIndex:
                    self._strokeIndex = 0
                    if self._strokeSeriesIndex >= self._numberOfStrokes:
                        self._countStrokesUp = False
                    if self._strokeSeriesIndex <= 1:
                        self._countStrokesUp = True
                    if self._countStrokesUp:
                        self._strokeSeriesIndex += 1
                    else:
                        self._strokeSeriesIndex -= 1
                    self._startDelay()
            else:
                self._nextMove.stroke = self._depth
                self._strokeIndex += 1
            self._nextMove.skip = False
        else:
            self._nextMove.skip = True
        self._index = index
        return self._nextMove


class Insist(Pattern):
    def __init__(self):
        super().__init__("Insist")
        self._speed = 0
        self._acceleration = 0
        self._realStroke = 0
        self._strokeFraction = 1.0
        self._strokeInFront = False

    def setSensation(self, sensation):
        self._sensation = sensation
        self._strokeFraction = (100.0 - abs(sensation)) / 100.0
        self._strokeInFront = (sensation > 0)
        self._updateStrokeTiming()

    def setTimeOfStroke(self, speed=0):
        self._timeOfStroke = 0.5 * speed
        self._updateStrokeTiming()

    def setStroke(self, stroke):
        self._stroke = stroke
        self._updateStrokeTiming()

    def _updateStrokeTiming(self):
        self._speed = int(1.5 * self._stroke / self._timeOfStroke)
        # Note: original C++ erroneously uses _nextMove.speed here; we use _speed
        self._acceleration = int(3.0 * self._speed / (self._timeOfStroke * self._strokeFraction))
        self._realStroke = int(float(self._stroke) * self._strokeFraction)

    def nextTarget(self, index):
        self._nextMove.acceleration = self._acceleration
        self._nextMove.speed = self._speed
        if self._strokeInFront:
            if index % 2:
                self._nextMove.stroke = self._depth - self._realStroke
            else:
                self._nextMove.stroke = self._depth
        else:
            if index % 2:
                self._nextMove.stroke = self._depth - self._stroke
            else:
                self._nextMove.stroke = (self._depth - self._stroke) + self._realStroke
        self._index = index
        return self._nextMove


patternTable = [
    SimpleStroke(),
    TeasingPounding(),
    RoboStroke(),
    HalfnHalf(),
    Deeper(),
    StopNGo(),
    Insist(),
]
patternTableSize = len(patternTable)


# =============================================================
#  SERVO STATE  (ported from StrokeEngine.h)
# =============================================================

class ServoState:
    UNDEFINED = 0
    READY = 1
    PATTERN = 2
    SETUPDEPTH = 3
    STREAMING = 4


# =============================================================
#  MACHINE GEOMETRY & MOTOR PROPERTIES  (ported from StrokeEngine.h)
# =============================================================

class machineGeometry:
    def __init__(self, physicalTravel=0.0, keepoutBoundary=0.0):
        self.physicalTravel = physicalTravel
        self.keepoutBoundary = keepoutBoundary


class motorProperties:
    def __init__(self, maxSpeed=0.0, maxAcceleration=0.0, stepsPerMillimeter=0.0,
                 invertDirection=False, enableActiveLow=True, stepPin=-1, directionPin=-1, enablePin=-1):
        self.maxSpeed = maxSpeed
        self.maxAcceleration = maxAcceleration
        self.stepsPerMillimeter = stepsPerMillimeter
        self.invertDirection = invertDirection
        self.enableActiveLow = enableActiveLow
        self.stepPin = stepPin
        self.directionPin = directionPin
        self.enablePin = enablePin


# =============================================================
#  MOTOR SIMULATOR  (extended to match FastAccelStepper API)
# =============================================================

class MotorSimulator:
    MODE_IDLE = 0
    MODE_MOVE_TO = 1
    MODE_RUN = 2

    def __init__(self):
        self.pos = 0.0
        self.vel = 0.0
        self.mode = self.MODE_IDLE
        self.target_pos = 0.0
        self.target_speed = float(JOG_SPEED_HZ)
        self.target_accel = float(JOG_ACCEL_HZ)
        self.run_dir = 1

    def set_speed_accel(self, speed_hz: float, accel_hz: float):
        self.target_speed = float(speed_hz)
        self.target_accel = float(accel_hz)

    def moveTo(self, target: int, speed_hz: Optional[float] = None,
               accel_hz: Optional[float] = None):
        self.mode = self.MODE_MOVE_TO
        self.target_pos = float(target)
        if speed_hz is not None:
            self.target_speed = float(speed_hz)
        if accel_hz is not None:
            self.target_accel = float(accel_hz)

    def runForward(self, speed_hz: Optional[float] = None,
                   accel_hz: Optional[float] = None):
        self.mode = self.MODE_RUN
        self.run_dir = 1
        if speed_hz is not None:
            self.target_speed = float(speed_hz)
        if accel_hz is not None:
            self.target_accel = float(accel_hz)

    def runBackward(self, speed_hz: Optional[float] = None,
                    accel_hz: Optional[float] = None):
        self.mode = self.MODE_RUN
        self.run_dir = -1
        if speed_hz is not None:
            self.target_speed = float(speed_hz)
        if accel_hz is not None:
            self.target_accel = float(accel_hz)

    def forceStop(self):
        self.mode = self.MODE_IDLE
        self.target_pos = self.pos
        self.vel = 0.0

    def forceStopAndNewPosition(self, new_pos: int):
        self.mode = self.MODE_IDLE
        self.pos = float(new_pos)
        self.target_pos = self.pos
        self.vel = 0.0

    def stopMove(self):
        """Decelerate to stop (like FastAccelStepper::stopMove)."""
        self.mode = self.MODE_IDLE
        self.target_pos = self.pos

    def getCurrentPosition(self) -> int:
        return int(round(self.pos))

    def getAcceleration(self) -> float:
        return self.target_accel

    def isRunning(self) -> bool:
        if self.mode == self.MODE_RUN:
            return True
        if self.mode == self.MODE_MOVE_TO:
            return abs(self.target_pos - self.pos) > 0.5 or abs(self.vel) > 0.5
        return False

    def update(self, dt: float):
        if self.mode == self.MODE_IDLE:
            if abs(self.vel) > 0.5:
                self._decelerate(dt)
            else:
                self.vel = 0.0
                self.pos = round(self.pos)

        elif self.mode == self.MODE_RUN:
            target_vel = self.target_speed * self.run_dir
            if abs(self.vel - target_vel) > 0.5:
                self._accelerate_to(target_vel, dt)
            else:
                self.vel = target_vel
            self.pos += self.vel * dt

        elif self.mode == self.MODE_MOVE_TO:
            delta = self.target_pos - self.pos
            if abs(delta) < 0.5 and abs(self.vel) < 0.5:
                self.pos = self.target_pos
                self.vel = 0.0
                self.mode = self.MODE_IDLE
                return

            direction = 1.0 if delta > 0 else -1.0

            if self.vel * direction < -0.5:
                self._decelerate(dt)
            else:
                stop_dist = (self.vel ** 2) / (2.0 * self.target_accel) \
                    if self.target_accel > 0 else 0.0

                if abs(delta) <= stop_dist and abs(self.vel) > 0.5:
                    self._decelerate(dt)
                else:
                    self._accelerate_to(self.target_speed * direction, dt)

            self.pos += self.vel * dt

            if (self.vel > 0 and self.pos > self.target_pos) or \
               (self.vel < 0 and self.pos < self.target_pos):
                self.pos = self.target_pos
                self.vel = 0.0
                self.mode = self.MODE_IDLE

    def _accelerate_to(self, target_vel: float, dt: float):
        if abs(target_vel - self.vel) < 0.5:
            self.vel = target_vel
            return
        dir = 1.0 if target_vel > self.vel else -1.0
        new_vel = self.vel + self.target_accel * dt * dir
        if dir > 0 and new_vel > target_vel:
            new_vel = target_vel
        elif dir < 0 and new_vel < target_vel:
            new_vel = target_vel
        self.vel = new_vel

    def _decelerate(self, dt: float):
        if abs(self.vel) < 0.5:
            self.vel = 0.0
            return
        dir = -1.0 if self.vel > 0 else 1.0
        new_vel = self.vel + self.target_accel * dt * dir
        if new_vel * self.vel < 0:
            new_vel = 0.0
        self.vel = new_vel


# =============================================================
#  MOCK FAST ACCEL STEPPER  (shim so StrokeEngine can drive MotorSimulator)
# =============================================================

class MockFastAccelStepper:
    def __init__(self, motor: MotorSimulator):
        self._motor = motor
        self._speed_hz = 0
        self._accel_hz = 0
        self._enabled = False

    def enableOutputs(self):
        self._enabled = True

    def disableOutputs(self):
        self._enabled = False
        self._motor.forceStop()

    def setCurrentPosition(self, pos: int):
        self._motor.forceStopAndNewPosition(int(pos))

    def getCurrentPosition(self) -> int:
        return self._motor.getCurrentPosition()

    def setSpeedInHz(self, speed: int):
        self._speed_hz = int(speed)

    def setAcceleration(self, accel: int):
        self._accel_hz = int(accel)

    def applySpeedAcceleration(self):
        self._motor.set_speed_accel(self._speed_hz, self._accel_hz)

    def moveTo(self, pos: int):
        self._motor.set_speed_accel(self._speed_hz, self._accel_hz)
        self._motor.moveTo(int(pos))

    def move(self, steps: int):
        target = self.getCurrentPosition() + int(steps)
        self.moveTo(target)

    def isRunning(self) -> bool:
        return self._motor.isRunning()

    def stopMove(self):
        self._motor.stopMove()

    def getAcceleration(self) -> int:
        return self._accel_hz

    def getSpeedInMilliHz(self) -> int:
        return self._speed_hz * 1000


# =============================================================
#  STROKE ENGINE  (ported from StrokeEngine.h / .cpp)
# =============================================================

class StrokeEngine:
    def __init__(self):
        self._state = ServoState.UNDEFINED
        self._motor = None
        self._physics = None
        self._servo = None
        self._travel = 0.0
        self._minStep = 0
        self._maxStep = 0
        self._maxStepPerSecond = 0
        self._maxStepAcceleration = 0
        self._patternIndex = 0
        self._isHomed = False
        self._index = 0
        self._depth = 0
        self._previousDepth = 0
        self._stroke = 0
        self._previousStroke = 0
        self._timeOfStroke = 1.0
        self._sensation = 0.0
        self._applyUpdate = False
        self._fancyAdjustment = False
        self._callbackTelemetry = None
        self._callBackHoming = None
        self._homeingSpeed = 0

    def begin(self, physics: machineGeometry, motor: motorProperties, servo: MockFastAccelStepper):
        self._physics = physics
        self._motor = motor
        self._servo = servo

        self._travel = self._physics.physicalTravel - (2 * self._physics.keepoutBoundary)
        self._minStep = 0
        self._maxStep = int(0.5 + self._travel * self._motor.stepsPerMillimeter)
        self._maxStepPerSecond = int(0.5 + self._motor.maxSpeed * self._motor.stepsPerMillimeter)
        self._maxStepAcceleration = int(0.5 + self._motor.maxAcceleration * self._motor.stepsPerMillimeter)

        self._state = ServoState.UNDEFINED
        self._isHomed = False
        self._patternIndex = 0
        self._index = 0
        self._depth = self._maxStep
        self._previousDepth = self._maxStep
        self._stroke = self._maxStep // 3
        self._previousStroke = self._maxStep // 3
        self._timeOfStroke = 1.0
        self._sensation = 0.0
        self._applyUpdate = False
        self._fancyAdjustment = False

    def setSpeed(self, speed: float, applyNow=False):
        if speed <= 0:
            speed = 0.5
        self._timeOfStroke = constrain(60.0 / speed, 0.01, 120.0)
        patternTable[self._patternIndex].setTimeOfStroke(self._timeOfStroke)
        if self._state == ServoState.PATTERN and applyNow:
            self._applyUpdate = True

    def getSpeed(self):
        return 60.0 / self._timeOfStroke

    def setDepth(self, depth: float, applyNow=False):
        self._depth = constrain(int(depth * self._motor.stepsPerMillimeter), self._minStep, self._maxStep)
        patternTable[self._patternIndex].setDepth(self._depth)
        if self._state == ServoState.PATTERN and applyNow:
            self._applyUpdate = True
        if self._state == ServoState.SETUPDEPTH:
            self._setupDepths()

    def getDepth(self):
        return self._depth / self._motor.stepsPerMillimeter

    def setStroke(self, stroke: float, applyNow=False):
        self._stroke = constrain(int(stroke * self._motor.stepsPerMillimeter), self._minStep, self._maxStep)
        patternTable[self._patternIndex].setStroke(self._stroke)
        if self._state == ServoState.PATTERN and applyNow:
            self._applyUpdate = True
        if self._state == ServoState.SETUPDEPTH:
            self._setupDepths()

    def getStroke(self):
        return self._stroke / self._motor.stepsPerMillimeter

    def setSensation(self, sensation: float, applyNow=False):
        self._sensation = constrain(sensation, -100.0, 100.0)
        patternTable[self._patternIndex].setSensation(self._sensation)
        if self._state == ServoState.PATTERN and applyNow:
            self._applyUpdate = True
        if self._state == ServoState.SETUPDEPTH:
            self._setupDepths()

    def getSensation(self):
        return self._sensation

    def setPattern(self, patternIndex: int, applyNow=False):
        if 0 <= patternIndex < patternTableSize and patternIndex != self._patternIndex:
            self._patternIndex = patternIndex
            patternTable[self._patternIndex].setSpeedLimit(
                self._maxStepPerSecond, self._maxStepAcceleration, int(self._motor.stepsPerMillimeter)
            )
            patternTable[self._patternIndex].setTimeOfStroke(self._timeOfStroke)
            patternTable[self._patternIndex].setStroke(self._stroke)
            patternTable[self._patternIndex].setDepth(self._depth)
            patternTable[self._patternIndex].setSensation(self._sensation)
            self._index = -1
            if self._state == ServoState.PATTERN and applyNow:
                self._applyUpdate = True
            return True
        return False

    def getPattern(self):
        return self._patternIndex

    def startPattern(self) -> bool:
        if self._state in (ServoState.READY, ServoState.SETUPDEPTH):
            if self._servo.isRunning():
                self._servo.setAcceleration(self._maxStepAcceleration)
                self._servo.applySpeedAcceleration()
                self._servo.stopMove()

            self._state = ServoState.PATTERN
            self._index = -1
            patternTable[self._patternIndex].setSpeedLimit(
                self._maxStepPerSecond, self._maxStepAcceleration, int(self._motor.stepsPerMillimeter)
            )
            patternTable[self._patternIndex].setTimeOfStroke(self._timeOfStroke)
            patternTable[self._patternIndex].setStroke(self._stroke)
            patternTable[self._patternIndex].setDepth(self._depth)
            patternTable[self._patternIndex].setSensation(self._sensation)
            return True
        return False

    def stopMotion(self):
        if self._state in (ServoState.PATTERN, ServoState.SETUPDEPTH):
            self._state = ServoState.READY
            self._servo.setAcceleration(self._maxStepAcceleration)
            self._servo.applySpeedAcceleration()
            self._servo.stopMove()
            if self._callbackTelemetry:
                self._callbackTelemetry(
                    self._servo.getCurrentPosition() / self._motor.stepsPerMillimeter,
                    0.0, False
                )

    def thisIsHome(self, speed=5.0):
        self._homeingSpeed = speed * self._motor.stepsPerMillimeter
        if self._state == ServoState.UNDEFINED:
            self._servo.enableOutputs()
            self._servo.setCurrentPosition(-int(self._motor.stepsPerMillimeter * self._physics.keepoutBoundary))
            self._servo.setSpeedInHz(int(self._homeingSpeed))
            self._servo.setAcceleration(self._maxStepAcceleration // 10)
            self._servo.moveTo(self._minStep)
            self._isHomed = True
            self._state = ServoState.READY
            return True
        return False

    def moveToMax(self, speed=10.0):
        if self._isHomed:
            self.stopMotion()
            speed_hz = max(1, min(int(speed * self._motor.stepsPerMillimeter), self._maxStepPerSecond))
            self._servo.setSpeedInHz(speed_hz)
            self._servo.setAcceleration(self._maxStepAcceleration // 10)
            self._servo.moveTo(self._maxStep)
            if self._callbackTelemetry:
                self._callbackTelemetry(self._maxStep / self._motor.stepsPerMillimeter, speed, False)
            return True
        return False

    def moveToMin(self, speed=10.0):
        if self._isHomed:
            self.stopMotion()
            speed_hz = max(1, min(int(speed * self._motor.stepsPerMillimeter), self._maxStepPerSecond))
            self._servo.setSpeedInHz(speed_hz)
            self._servo.setAcceleration(self._maxStepAcceleration // 10)
            self._servo.moveTo(self._minStep)
            if self._callbackTelemetry:
                self._callbackTelemetry(self._minStep / self._motor.stepsPerMillimeter, speed, False)
            return True
        return False

    def setupDepth(self, speed=10.0, fancy=False):
        self._fancyAdjustment = fancy
        if self._isHomed:
            self.stopMotion()
            speed_hz = max(1, min(int(speed * self._motor.stepsPerMillimeter), self._maxStepPerSecond))
            self._servo.setSpeedInHz(speed_hz)
            self._servo.setAcceleration(self._maxStepAcceleration // 10)
            self._state = ServoState.SETUPDEPTH
            self._setupDepths()
            return True
        return False

    def getState(self):
        return self._state

    def disable(self):
        self._state = ServoState.UNDEFINED
        self._isHomed = False
        self._servo.disableOutputs()

    def getPatternName(self, index):
        if 0 <= index < patternTableSize:
            return patternTable[index].getName()
        return "Invalid"

    def getNumberOfPattern(self):
        return patternTableSize

    def setMaxSpeed(self, maxSpeed):
        self._maxStepPerSecond = int(0.5 + self._motor.maxSpeed * self._motor.stepsPerMillimeter)
        patternTable[self._patternIndex].setSpeedLimit(
            self._maxStepPerSecond, self._maxStepAcceleration, int(self._motor.stepsPerMillimeter)
        )

    def getMaxSpeed(self):
        return self._maxStepPerSecond / self._motor.stepsPerMillimeter

    def setMaxAcceleration(self, maxAcceleration):
        self._maxStepAcceleration = int(0.5 + self._motor.maxAcceleration * self._motor.stepsPerMillimeter)
        patternTable[self._patternIndex].setSpeedLimit(
            self._maxStepPerSecond, self._maxStepAcceleration, int(self._motor.stepsPerMillimeter)
        )

    def getMaxAcceleration(self):
        return self._maxStepAcceleration / self._motor.stepsPerMillimeter

    def registerTelemetryCallback(self, callback):
        self._callbackTelemetry = callback

    # -----------------------------------------------------------------
    #  Internal stroking loop — called from DeviceEmulator at 100 Hz
    # -----------------------------------------------------------------
    def update(self, dt=None):
        if self._state != ServoState.PATTERN:
            return

        if self._applyUpdate:
            currentMotion = patternTable[self._patternIndex].nextTarget(self._index)
            if self._servo.getAcceleration() > currentMotion.acceleration:
                currentMotion.acceleration = int(self._servo.getAcceleration())
            self._applyMotionProfile(currentMotion)
            self._applyUpdate = False
            return

        if not self._servo.isRunning():
            self._index += 1
            currentMotion = patternTable[self._patternIndex].nextTarget(self._index)
            if not currentMotion.skip:
                self._applyMotionProfile(currentMotion)
            else:
                self._index -= 1

    def _applyMotionProfile(self, motion: motionParameter):
        if motion.skip:
            return

        clipping = False
        if motion.speed > self._maxStepPerSecond:
            motion.speed = self._maxStepPerSecond
            clipping = True

        if motion.acceleration > self._maxStepAcceleration:
            motion.acceleration = self._maxStepAcceleration
            clipping = True

        pos = constrain(motion.stroke, self._minStep, self._maxStep)

        self._servo.setSpeedInHz(motion.speed)
        self._servo.setAcceleration(motion.acceleration)
        self._servo.moveTo(pos)

        speed_mm = motion.speed / self._motor.stepsPerMillimeter
        pos_mm = pos / self._motor.stepsPerMillimeter

        if self._callbackTelemetry:
            self._callbackTelemetry(pos_mm, speed_mm, clipping)

    def _setupDepths(self):
        depth = self._depth
        if self._fancyAdjustment:
            depth = int(fmap(self._sensation, -100, 100, self._depth - self._stroke, self._depth))
        self._servo.moveTo(depth)
        if self._callbackTelemetry:
            self._callbackTelemetry(
                depth / self._motor.stepsPerMillimeter,
                self._servo.getSpeedInMilliHz() / 1000.0 / self._motor.stepsPerMillimeter,
                False
            )


# =============================================================
#  DEVICE EMULATOR
# =============================================================

class DeviceEmulator:
    def __init__(self, serial_port=None):
        self.MM_PER_STEP = DEFAULT_MM_PER_STEP
        self.motor = MotorSimulator()
        self.stroke_engine = None
        self._stepper_mock = None

        self._serial = None
        if serial_port and serial:
            try:
                self._serial = serial.Serial(serial_port, 115200, timeout=0.1, write_timeout=1.0)
                print(f"[SERIAL] Opened {serial_port}")
            except Exception as e:
                print(f"[SERIAL] Failed to open {serial_port}: {e}")

        self.g_zeroPos = 0
        self.g_maxPos = TOTAL_STEPS_PER_REV
        self.g_homed = False
        self.g_engineReady = False
        self.g_running = False

        self.g_strokePct = 80.0
        self.g_depthPct = 100.0
        self.g_speedPct = 30.0
        self.g_sensation = 0.0
        self.g_patternIdx = 0

        self.g_calibrating = False
        self.g_calibStartSteps = 0
        self.pending_setmax = False

        self.serial_queue: queue.Queue = queue.Queue()
        self.serial_write_queue: queue.Queue = queue.Queue(maxsize=50)

    def current_steps(self) -> int:
        return self.motor.getCurrentPosition()

    def rail_length_mm(self) -> float:
        return float(self.g_maxPos - self.g_zeroPos) * self.MM_PER_STEP

    def pattern_speed_from_pct(self, pct: float) -> float:
        n = constrain(pct, 0.0, 100.0) / 100.0
        return 10.0 + (MAX_SPEED_MM_S - 10.0) * n * n

    def spm_from_linear_speed(self, stroke_mm: float, linear_mm_s: float) -> float:
        cycle_distance_mm = max(2.0 * stroke_mm, 1.0)
        return (constrain(linear_mm_s, 1.0, MAX_SPEED_MM_S) * 60.0) / cycle_distance_mm

    def pattern_speed_cap_for_stroke_mm(self, stroke_mm: float) -> float:
        cycle_distance_mm = max(2.0 * stroke_mm, 1.0)
        cap_spm = (MAX_SPEED_MM_S * 60.0) / cycle_distance_mm
        return constrain(cap_spm, MIN_PATTERN_SPEED_SPM, MAX_PATTERN_SPEED_SPM)

    # ── Broadcast (non-blocking) ─────────────────────────────────────────────

    def broadcast_raw(self, json_str: str):
        print(json_str, flush=True)
        if self._serial and self._serial.is_open:
            try:
                self.serial_write_queue.put_nowait(json_str)
            except queue.Full:
                print("[SERIAL] Write queue full, dropping message")

    def broadcast_doc(self, doc: dict):
        self.broadcast_raw(json.dumps(doc))

    def broadcast_position(self):
        abs_steps = self.current_steps()
        range_size = self.g_maxPos - self.g_zeroPos
        if range_size > 0:
            pct = ((abs_steps - self.g_zeroPos) / float(range_size)) * 100.0
            pct = constrain(pct, 0.0, 100.0)
        else:
            pct = 0.0

        self.broadcast_doc({
            "type": "position",
            "steps": abs_steps,
            "pct": round(pct, 2),
            "maxSteps": range_size,
            "homed": self.g_homed,
            "running": self.g_running,
            "engineReady": self.g_engineReady
        })

    # ── StrokeEngine ─────────────────────────────────────────────────────────

    def init_stroke_engine(self):
        physics = machineGeometry(
            physicalTravel=self.rail_length_mm(),
            keepoutBoundary=3.0
        )
        motor = motorProperties(
            maxSpeed=MAX_SPEED_MM_S,
            maxAcceleration=3000.0,
            stepsPerMillimeter=1.0 / self.MM_PER_STEP,
            invertDirection=False,
            enableActiveLow=True,
            stepPin=-1,
            directionPin=-1,
            enablePin=-1
        )
        self._stepper_mock = MockFastAccelStepper(self.motor)
        self.stroke_engine = StrokeEngine()
        self.stroke_engine.begin(physics, motor, self._stepper_mock)
        self.stroke_engine.thisIsHome(5.0)
        self.stroke_engine.setPattern(self.g_patternIdx, False)
        self.g_engineReady = True
        print(f"[SE] Init — rail {self.rail_length_mm():.1f} mm, "
              f"{self.MM_PER_STEP:.4f} mm/step, vMax {MAX_SPEED_MM_S:.1f} mm/s")

    def push_params_to_engine(self):
        if not self.g_engineReady or self.stroke_engine is None:
            return
        rl = self.rail_length_mm()
        dep = (self.g_depthPct / 100.0) * rl
        strk = (self.g_strokePct / 100.0) * rl
        linear_req = self.pattern_speed_from_pct(self.g_speedPct)
        spd_req = self.spm_from_linear_speed(strk, linear_req)
        spd_cap = self.pattern_speed_cap_for_stroke_mm(strk)
        spd = min(spd_req, spd_cap)

        self.stroke_engine.setSpeed(spd, False)
        self.stroke_engine.setDepth(dep, False)
        self.stroke_engine.setStroke(strk, False)
        self.stroke_engine.setSensation(self.g_sensation, False)

    # ── Command handler ──────────────────────────────────────────────────────

    def handle_message(self, raw: str):
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            return

        cmd = doc.get("cmd", "")

        if cmd == "reboot":
            print("[CMD] Reboot requested")
            self.broadcast_doc({"type": "rebooting"})
            asyncio.create_task(self._do_reboot())
            return

        if cmd == "stop":
            if self.g_running and self.stroke_engine:
                self.stroke_engine.stopMotion()
                self.g_running = False
            self.motor.forceStopAndNewPosition(self.current_steps())
            print("[CMD] STOP")

        elif cmd == "jogFwd":
            self.motor.set_speed_accel(JOG_SPEED_HZ, JOG_ACCEL_HZ)
            self.motor.runForward()

        elif cmd == "jogBwd":
            self.motor.set_speed_accel(JOG_SPEED_HZ, JOG_ACCEL_HZ)
            self.motor.runBackward()

        elif cmd == "setZero":
            self.motor.forceStopAndNewPosition(0)
            self.g_zeroPos = 0
            self.g_engineReady = False
            self.g_running = False
            self.pending_setmax = False
            if self.g_maxPos <= self.g_zeroPos:
                self.g_maxPos = self.g_zeroPos + TOTAL_STEPS_PER_REV
            self.g_homed = True
            self.broadcast_position()
            print("[CMD] Zero set")

        elif cmd == "setMax":
            measured_pos = self.current_steps()
            travel_steps = abs(measured_pos - self.g_zeroPos)
            if travel_steps < 50:
                print(f"[CMD] Max ignored: travel too small ({travel_steps} steps)")
                return
            self.g_maxPos = self.g_zeroPos + travel_steps
            self.motor.set_speed_accel(JOG_SPEED_HZ, JOG_ACCEL_HZ)
            self.motor.moveTo(self.g_zeroPos)
            self.pending_setmax = True

        elif cmd == "moveTo":
            if self.g_running:
                return
            pct = constrain(doc.get("pct", 50.0), 0.0, 100.0)
            back_pct = doc.get("back", 0.0)
            front_pct = doc.get("front", 100.0)
            speed_pct = constrain(doc.get("speedPct", 60.0), 1.0, 100.0)
            accel_pct = constrain(doc.get("accelPct", 60.0), 1.0, 100.0)

            range_steps = float(self.g_maxPos - self.g_zeroPos)
            back_abs = self.g_zeroPos + (back_pct / 100.0) * range_steps
            front_abs = self.g_zeroPos + (front_pct / 100.0) * range_steps
            target = int(back_abs + (pct / 100.0) * (front_abs - back_abs))

            speed_hz = int(MANUAL_SPEED_MIN_HZ + (speed_pct / 100.0) *
                           (MANUAL_SPEED_MAX_HZ - MANUAL_SPEED_MIN_HZ))
            accel_hz = int(MANUAL_ACCEL_MIN_HZ + (accel_pct / 100.0) *
                           (MANUAL_ACCEL_MAX_HZ - MANUAL_ACCEL_MIN_HZ))

            target = constrain(target, self.g_zeroPos, self.g_maxPos)
            self.motor.moveTo(target, speed_hz, accel_hz)

        elif cmd == "stream":
            if self.g_running and self.stroke_engine:
                self.stroke_engine.stopMotion()
                self.g_running = False

            pct = constrain(doc.get("pct", 0.0), 0.0, 100.0)
            duration_ms = doc.get("duration", 0)

            range_steps = self.g_maxPos - self.g_zeroPos
            target_steps = self.g_zeroPos + int((pct / 100.0) * range_steps)
            delta_steps = abs(target_steps - self.current_steps())

            if duration_ms <= 0 or delta_steps == 0:
                self.motor.moveTo(target_steps, STREAM_MAX_SPEED_HZ, STREAM_ACCEL_HZ)
            else:
                time_sec = duration_ms / 1000.0
                required_speed = int(delta_steps / time_sec)
                required_speed = constrain(required_speed, 10, STREAM_MAX_SPEED_HZ)
                self.motor.moveTo(target_steps, required_speed, STREAM_ACCEL_HZ)

        elif cmd == "startPattern":
            if not self.g_engineReady:
                print("[SE] Not ready — home first")
                return
            self.push_params_to_engine()
            if self.stroke_engine.startPattern():
                self.g_running = True
                print("[SE] Pattern started")
            else:
                self.g_running = False
                print(f"[SE] Pattern start failed (state={self.stroke_engine.getState()})")

        elif cmd == "stopPattern":
            if self.stroke_engine:
                self.stroke_engine.stopMotion()
            self.g_running = False
            print("[SE] Pattern stopped")

        elif cmd == "setPattern":
            self.g_patternIdx = constrain(doc.get("value", 0), 0,
                                          self.stroke_engine.getNumberOfPattern() - 1 if self.stroke_engine else 6)
            if self.g_engineReady and self.stroke_engine:
                self.stroke_engine.setPattern(self.g_patternIdx, False)
            print(f"[SE] Pattern → {self.g_patternIdx}")

        elif cmd == "setStrokePct":
            self.g_strokePct = constrain(doc.get("value", 50.0), 1.0, 100.0)
            if self.g_engineReady and self.stroke_engine:
                self.stroke_engine.setStroke((self.g_strokePct / 100.0) * self.rail_length_mm(), False)

        elif cmd == "setDepthPct":
            self.g_depthPct = constrain(doc.get("value", 100.0), 1.0, 100.0)
            if self.g_engineReady and self.stroke_engine:
                self.stroke_engine.setDepth((self.g_depthPct / 100.0) * self.rail_length_mm(), False)

        elif cmd == "setSpeedPct":
            self.g_speedPct = constrain(doc.get("value", 30.0), 1.0, 100.0)
            if self.g_engineReady and self.stroke_engine:
                strk = (self.g_strokePct / 100.0) * self.rail_length_mm()
                linear_req = self.pattern_speed_from_pct(self.g_speedPct)
                spd_req = self.spm_from_linear_speed(strk, linear_req)
                spd_cap = self.pattern_speed_cap_for_stroke_mm(strk)
                self.stroke_engine.setSpeed(min(spd_req, spd_cap), False)

        elif cmd == "setSensation":
            self.g_sensation = constrain(doc.get("value", 0.0), -100.0, 100.0)
            if self.g_engineReady and self.stroke_engine:
                self.stroke_engine.setSensation(self.g_sensation, False)

        elif cmd == "calibStart":
            if self.g_running and self.stroke_engine:
                self.stroke_engine.stopMotion()
                self.g_running = False
            self.motor.forceStopAndNewPosition(self.current_steps())
            self.g_calibStartSteps = self.current_steps()
            self.g_calibrating = True
            self.motor.set_speed_accel(JOG_SPEED_HZ, JOG_ACCEL_HZ)
            self.motor.moveTo(self.g_calibStartSteps +
                              (CALIB_MOVE_SIGN * TOTAL_STEPS_PER_REV))
            print("[CALIB] Moving 1 rev forward — measure travel, then enter mm")

        elif cmd == "calibSet":
            if self.motor.isRunning():
                print("[CALIB] Wait for 1 rev move to finish before SET")
                return
            measured_mm = doc.get("mm", 40.0)
            if measured_mm > 1.0:
                self.MM_PER_STEP = measured_mm / float(TOTAL_STEPS_PER_REV)
                self.g_calibrating = False
                print(f"[CALIB] MM_PER_STEP set to {self.MM_PER_STEP:.5f} "
                      f"({measured_mm:.2f} mm/rev)")
                self.broadcast_doc({
                    "type": "calibAck",
                    "mmPerStep": self.MM_PER_STEP,
                    "mmPerRev": measured_mm,
                    "note": "scale updated; limits unchanged"
                })

        elif cmd == "wiggle":
            delta = TOTAL_STEPS_PER_REV // 4
            self.motor.set_speed_accel(JOG_SPEED_HZ * 2, JOG_ACCEL_HZ * 2)
            self.motor.moveTo(self.current_steps() + delta)

    async def _do_reboot(self):
        await asyncio.sleep(0.15)
        self.__init__()
        print("\n=== ESS57 / ESP32-C3 OSSM + StrokeEngine ===")
        print("[SERIAL] JSON command interface active on stdio/serial")
        print("[SERIAL] Send one JSON object per line, e.g.:")
        print("[SERIAL]   {\"cmd\":\"jogFwd\"}")
        print("[SERIAL]   {\"cmd\":\"setZero\"}")
        print("[SERIAL]   {\"cmd\":\"stop\"}")
        await self.startup_wiggle()

    # ── I/O threads (decoupled from asyncio) ─────────────────────────────────

    def serial_reader_thread(self):
        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if line.startswith('{'):
                    self.serial_queue.put(line)
            except Exception as e:
                print(f"[STDIN] Error: {e}")

    def serial_port_reader_thread(self):
        if not self._serial:
            return
        buffer = ""
        while True:
            try:
                if self._serial.in_waiting:
                    data = self._serial.read(self._serial.in_waiting)
                    buffer += data.decode('utf-8', errors='ignore')
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        if line.startswith('{'):
                            self.serial_queue.put(line)
                else:
                    time.sleep(0.01)
            except Exception as e:
                print(f"[SERIAL] Read error: {e}")
                time.sleep(0.5)

    def serial_writer_thread(self):
        while True:
            try:
                json_str = self.serial_write_queue.get(timeout=0.2)
                if self._serial and self._serial.is_open:
                    try:
                        self._serial.write((json_str + '\n').encode('utf-8'))
                    except Exception as e:
                        print(f"[SERIAL] Write error: {e}")
            except queue.Empty:
                continue
            except Exception as e:
                print(f"[SERIAL] Writer error: {e}")

    async def serial_loop(self):
        while True:
            try:
                line = self.serial_queue.get_nowait()
                self.handle_message(line)
            except queue.Empty:
                await asyncio.sleep(0.01)

    # ── Startup & background loops ───────────────────────────────────────────

    async def startup_wiggle(self):
        print("[WIGGLE] Starting")
        self.motor.set_speed_accel(300, 600)
        self.motor.moveTo(TOTAL_STEPS_PER_REV // 4)
        while self.motor.isRunning():
            await asyncio.sleep(0.01)

        self.motor.moveTo(-TOTAL_STEPS_PER_REV // 4)
        while self.motor.isRunning():
            await asyncio.sleep(0.01)

        self.motor.moveTo(0)
        while self.motor.isRunning():
            await asyncio.sleep(0.01)

        self.motor.set_speed_accel(JOG_SPEED_HZ, JOG_ACCEL_HZ)
        print("[WIGGLE] Done")

    async def motor_loop(self):
        while True:
            self.motor.update(PHYSICS_DT)
            if self.stroke_engine:
                self.stroke_engine.update(PHYSICS_DT)

            if self.pending_setmax and not self.motor.isRunning():
                self.init_stroke_engine()
                self.pending_setmax = False
                self.broadcast_position()
                print(f"[CMD] Max set → {self.g_maxPos} steps "
                      f"({self.rail_length_mm():.1f} mm)")

            await asyncio.sleep(PHYSICS_DT)

    async def broadcast_loop(self):
        while True:
            try:
                self.broadcast_position()
            except Exception as exc:
                print(f"[BROADCAST ERROR] {exc}", flush=True)
            await asyncio.sleep(0.02)  # 50 Hz

    async def run(self):
        loop = asyncio.get_running_loop()
        def _exception_handler(loop, context):
            exc = context.get("exception")
            print(f"\n[ASYNC EXCEPTION] {exc}", flush=True)
            import traceback
            traceback.print_exception(type(exc), exc, exc.__traceback__)
        loop.set_exception_handler(_exception_handler)

        print("\n=== ESS57 / ESP32-C3 OSSM + StrokeEngine ===")
        print("[SERIAL] JSON command interface active on stdio/serial")
        print("[SERIAL] Send one JSON object per line, e.g.:")
        print("[SERIAL]   {\"cmd\":\"jogFwd\"}")
        print("[SERIAL]   {\"cmd\":\"setZero\"}")
        print("[SERIAL]   {\"cmd\":\"stop\"}")

        threading.Thread(target=self.serial_reader_thread, daemon=True).start()
        if self._serial:
            threading.Thread(target=self.serial_port_reader_thread, daemon=True).start()
            threading.Thread(target=self.serial_writer_thread, daemon=True).start()

        asyncio.create_task(self.motor_loop())
        asyncio.create_task(self.broadcast_loop())
        asyncio.create_task(self.serial_loop())

        await self.startup_wiggle()

        print("[DEVICE] Emulator ready. Reading commands from stdin/serial.")
        await asyncio.Future()


# =============================================================
#  ENTRY POINT
# =============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--serial', default=None, help='Serial port for I/O (e.g. /dev/pts/5)')
    args = parser.parse_args()

    emulator = DeviceEmulator(serial_port=args.serial)
    try:
        asyncio.run(emulator.run())
    except KeyboardInterrupt:
        print("\n[EXIT] Shutting down emulator")

if __name__ == "__main__":
    main()