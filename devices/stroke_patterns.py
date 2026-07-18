"""
devices/stroke_patterns.py
--------------------------
Percent-space port of the OSSM firmware stroke patterns (pattern.h).

The OSSM board generates stroke motion itself: the host sends a pattern index
plus speed/depth/stroke/sensation and the firmware's engine emits the individual
moves. Buttplug devices have no such engine — Intiface expects the host to hand
it an explicit position stream — so the seven patterns are reproduced here and
fed into the ticker in devices/buttplug.py.

Positions are percentages (0 = fully out, 100 = fully in) instead of the
firmware's millimetres, which makes the maths unit-free; the *timings* are
preserved exactly, so a pattern feels the same on a Handy as it does on OSSM.

Deliberately kept close to the port in device_emulator.py — same class names,
same method names, same structure — so the two can be diffed when a pattern is
tweaked. The differences are only: percent units, no acceleration (LinearCmd
takes position+duration, not accel), and a next_move() wrapper that converts the
firmware's speed into a move duration.
"""

import math
import time
from dataclasses import dataclass
from typing import Optional

# Speed curve, mirroring device_emulator.pattern_speed_from_pct: quadratic, so
# the low end of the dial has usable resolution.
MIN_SPM = 6.0
MAX_SPM = 300.0

MIN_MOVE_MS = 20
MAX_MOVE_MS = 10_000


def constrain(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def fmap(x, in_min, in_max, out_min, out_max):
    return (x - in_min) * (out_max - out_min) / (in_max - in_min) + out_min


def fscale(originalMin, originalMax, newBegin, newEnd, inputValue, curve):
    """Arduino fscale() — logarithmic-ish remap used by the sensation curves."""
    curve = constrain(curve, -10, 10)
    curve = math.pow(10, curve * -0.1)

    inputValue = constrain(inputValue, originalMin, originalMax)

    OriginalRange = originalMax - originalMin
    if OriginalRange == 0:
        return 0

    if newEnd > newBegin:
        NewRange = newEnd - newBegin
        invFlag = False
    else:
        NewRange = newBegin - newEnd
        invFlag = True

    normalizedCurVal = (inputValue - originalMin) / OriginalRange

    if originalMin > originalMax:
        return 0

    if not invFlag:
        return (math.pow(normalizedCurVal, curve) * NewRange) + newBegin
    return newBegin - (math.pow(normalizedCurVal, curve) * NewRange)


def cycle_seconds(speed_pct: float) -> float:
    """Speed dial (0-100) -> seconds for one full in+out cycle."""
    n = constrain(speed_pct, 0.0, 100.0) / 100.0
    spm = MIN_SPM + (MAX_SPM - MIN_SPM) * n * n
    return 60.0 / spm


@dataclass
class Move:
    """One leg of a stroke: where to go and how long to take getting there."""
    target: float       # 0-100
    duration_ms: int
    skip: bool = False  # pattern is pausing (Stop'n'Go); hold position


class motionParameter:
    def __init__(self, stroke=0.0, speed=0.0, acceleration=0.0, skip=False):
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
        self._stroke = 0.0
        self._depth = 0.0
        self._timeOfStroke = 1.0
        self._sensation = 0.0
        self._index = -1
        self._nextMove = motionParameter()
        self._startDelayMillis = 0
        self._delayInMillis = 0

    # ── Firmware API ─────────────────────────────────────────────────────────

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

    def _startDelay(self):
        self._startDelayMillis = int(time.time() * 1000)

    def _updateDelay(self, delayInMillis):
        self._delayInMillis = delayInMillis

    def _isStillDelayed(self):
        return int(time.time() * 1000) < (self._startDelayMillis + self._delayInMillis)

    # ── Host-side wrapper ────────────────────────────────────────────────────

    def set_params(self, speed_pct: float, depth_pct: float,
                   base_pct: float, sensation: float) -> None:
        """Apply AIMO's (speed, depth, base, intensity) in one shot.

        Order matters: Insist recomputes its timing inside setStroke(), so
        _timeOfStroke has to be valid first.
        """
        depth = constrain(depth_pct, 0.0, 100.0)
        base = constrain(base_pct, 0.0, 100.0)
        self.setTimeOfStroke(cycle_seconds(speed_pct))
        self.setDepth(depth)
        self.setStroke(max(0.0, depth - base))
        self.setSensation(constrain(sensation, -100.0, 100.0))

    def next_move(self, index: int, current: float) -> Move:
        """Next leg of the stroke, as an absolute target + travel time.

        The firmware drives a trapezoidal profile whose *peak* speed is 1.5x the
        average, so the time to cover `dist` at reported speed `v` is
        1.5 * dist / v — which is what reproduces the original stroke timing.
        """
        if self._stroke <= 0.0:
            return Move(target=current, duration_ms=MIN_MOVE_MS, skip=True)

        mp = self.nextTarget(index)
        if getattr(mp, "skip", False):
            return Move(target=current, duration_ms=MIN_MOVE_MS, skip=True)

        target = constrain(mp.stroke, 0.0, 100.0)
        speed = max(float(mp.speed), 1e-6)
        dist = abs(target - current)
        duration_ms = int(1000.0 * 1.5 * dist / speed)
        return Move(
            target=target,
            duration_ms=int(constrain(duration_ms, MIN_MOVE_MS, MAX_MOVE_MS)),
        )


class SimpleStroke(Pattern):
    def __init__(self):
        super().__init__("Simple Stroke")

    def setTimeOfStroke(self, speed=0):
        self._timeOfStroke = 0.5 * speed

    def nextTarget(self, index):
        self._nextMove.speed = 1.5 * self._stroke / self._timeOfStroke
        self._nextMove.acceleration = 3.0 * self._nextMove.speed / self._timeOfStroke
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
        self._timeOfFastStroke = (0.5 * self._timeOfStroke) / fscale(
            0.0, 100.0, 1.0, 5.0, abs(self._sensation), 0.0)
        if self._sensation > 0.0:
            self._timeOfInStroke = self._timeOfFastStroke
            self._timeOfOutStroke = self._timeOfStroke - self._timeOfFastStroke
        else:
            self._timeOfOutStroke = self._timeOfFastStroke
            self._timeOfInStroke = self._timeOfStroke - self._timeOfFastStroke

    def nextTarget(self, index):
        if index % 2:
            t = max(self._timeOfOutStroke, 1e-6)
            self._nextMove.speed = 1.5 * self._stroke / t
            self._nextMove.stroke = self._depth - self._stroke
        else:
            t = max(self._timeOfInStroke, 1e-6)
            self._nextMove.speed = 1.5 * self._stroke / t
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
        speed = self._stroke / max((1.0 - self._x) * self._timeOfStroke, 1e-6)
        self._nextMove.speed = speed
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
        self._timeOfFastStroke = (0.5 * self._timeOfStroke) / fscale(
            0.0, 100.0, 1.0, 5.0, abs(self._sensation), 0.0)
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
            stroke = self._stroke / 2.0
        if index % 2:
            t = max(self._timeOfOutStroke, 1e-6)
            self._nextMove.speed = 1.5 * stroke / t
            self._nextMove.stroke = self._depth - self._stroke
            self._half = not self._half
        else:
            t = max(self._timeOfInStroke, 1e-6)
            self._nextMove.speed = 1.5 * stroke / t
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
        self._countStrokesForRamp = max(1, self._countStrokesForRamp)

    def nextTarget(self, index):
        slope = self._stroke / self._countStrokesForRamp
        cycleIndex = (index // 2) % self._countStrokesForRamp + 1
        amplitude = slope * cycleIndex
        self._nextMove.speed = 1.5 * amplitude / max(self._timeOfStroke, 1e-6)
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
        self._nextMove.speed = 1.5 * self._stroke / max(self._timeOfStroke, 1e-6)
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
        self._speed = 0.0
        self._realStroke = 0.0
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
        self._speed = 1.5 * self._stroke / max(self._timeOfStroke, 1e-6)
        self._realStroke = self._stroke * self._strokeFraction

    def nextTarget(self, index):
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


# Names match config.AI_TO_DEVICE_PATTERN_MAP, which is what the intent compiler
# emits; the OSSM driver turns those into firmware indices, we turn them into
# classes.
_PATTERN_CLASSES = {
    "simple_stroke": SimpleStroke,
    "teasing_and_pounding": TeasingPounding,
    "robo_stroke": RoboStroke,
    "half_n_half": HalfnHalf,
    "deeper": Deeper,
    "stop_n_go": StopNGo,
    "insist": Insist,
}


def make_pattern(name: str) -> Optional[Pattern]:
    """Instantiate a pattern by AIMO name. Returns None for unknown names."""
    cls = _PATTERN_CLASSES.get(name)
    return cls() if cls else None
