"""
devices/buttplug.py
-------------------
Buttplug v3 client, talking to Intiface Central over WebSocket.

Intiface Central (https://intiface.com/central/) exposes a Buttplug server on
ws://127.0.0.1:12345 and handles the per-toy protocols itself, so this driver
only has to speak the wire protocol — no vendor-specific work. The v3 message
subset we need is small enough (~10 messages) that hand-rolling it on the
`websockets` dependency we already have beats pulling in buttplug-py, and it
keeps the PyInstaller bundle small.

Motion model
------------
Everything funnels through a single position signal (0 = out, 100 = in), driven
by one of two sources:

  * stream mode  — explicit targets from the funscript player / manual moveTo
  * pattern mode — the AI session's (pattern, speed, depth, base, intensity),
                   rendered locally by devices/stroke_patterns.py

That signal then fans out to whatever the toy actually has, and the two output
paths are deliberately asymmetric because the commands are:

  * LinearCmd is *sparse*. It carries a duration and the toy interpolates the
    travel itself, so it is sent once per move. Re-sending it every tick would
    fight the toy's own interpolation and produce stutter.
  * ScalarCmd is *instantaneous*. It sets vibration now and holds until the
    next message, so it gets the densely interpolated stream from the ticker —
    otherwise a 400ms move lands as one flat step instead of a swell.

This is what lets a vibrator be driven as if it were a stroker: position maps
straight onto vibration strength, so a stroke pattern becomes a pulse envelope.

Rate limiting is not optional here — BLE toys choke on command floods, which is
the usual cause of Buttplug stutter and dropped links. Hence TICK_HZ plus a
deadband on scalar output.
"""

import asyncio
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

import websockets

from .base import AbstractDevice
from .stroke_patterns import make_pattern

try:
    from settings_store import load_settings
except Exception:  # pragma: no cover - settings are optional for the device layer
    load_settings = None  # type: ignore

log = logging.getLogger(__name__)

DEFAULT_WS_URL = "ws://127.0.0.1:12345"
CLIENT_NAME = "AIMO"
MESSAGE_VERSION = 3

TICK_HZ = 20
TICK_INTERVAL = 1.0 / TICK_HZ
SCALAR_DEADBAND = 0.02          # don't resend for changes the toy can't render
POSITION_EMIT_INTERVAL = 0.1    # throttle UI/SSE updates to 10Hz

RECONNECT_DELAY_INITIAL = 1.0
RECONNECT_DELAY_MAX = 30.0

# Scalar actuators that should follow the stroke signal. Constrict/Inflate are
# deliberately excluded: sweeping a constrictor 0->1 at 20Hz would be unpleasant
# and is hard on the hardware. Oscillate is included because on devices that
# implement it, it is genuine reciprocating motion — closer to stroking than
# vibration is.
DRIVEN_SCALAR_ACTUATORS = {"Vibrate", "Oscillate"}


def _settings() -> dict:
    if load_settings is None:
        return {}
    try:
        return load_settings()
    except Exception:
        return {}


def _configured_url() -> str:
    url = str(_settings().get("buttplug_ws_url", "") or "").strip()
    return url or DEFAULT_WS_URL


def _configured_floor() -> float:
    """Minimum vibration for a non-zero position.

    Motors take 50-100ms to spin up, so a full 0->1->0 sweep at speed never
    quite reaches either end and turns to mush. Raising the floor keeps pulses
    distinct at the cost of never fully stopping between strokes. Defaults to 0
    (a true 0 -> 0.0 mapping); set buttplug_vibe_floor to ~0.15 if fast patterns
    feel indistinct.
    """
    try:
        return max(0.0, min(0.9, float(_settings().get("buttplug_vibe_floor", 0.0))))
    except (TypeError, ValueError):
        return 0.0


class ButtplugDevice(AbstractDevice):
    name = "Buttplug / Intiface"
    device_type = "buttplug"

    def __init__(self):
        super().__init__()
        self._ws_url: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ws: Optional[Any] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

        self._msg_id = 1
        self._max_ping_ms = 0
        self._connection_attempts = 0

        # Discovered toys: device_index -> {name, scalars, linears, rotates}
        self._devices: Dict[int, dict] = {}
        # Which of them to drive. Empty = all of them.
        self._selected: set = set()

        # Motion state
        self._mode = "idle"           # idle | pattern | stream
        self._pos = 0.0               # 0-100
        self._move_from = 0.0
        self._move_to = 0.0
        self._move_start = 0.0        # time.monotonic()
        self._move_dur = 0.0          # seconds
        self._move_active = False
        self._pending_linear = False  # a new move needs one LinearCmd

        self._pattern = None
        self._pattern_index = 0
        self._speed = 50.0
        self._depth = 50.0
        self._base = 0.0
        self._sensation = 0.0

        self._vibe_floor = _configured_floor()
        self._last_scalar: Dict[int, float] = {}
        self._last_direction = 1
        self._last_emit = 0.0

        self._update_state(
            pct=0.0, running=False, homed=True, engineReady=False,
            device_count=0, devices=[],
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self, address: Optional[str] = None) -> bool:
        if self._state.connected:
            self.disconnect()

        self._ws_url = (address or "").strip() or _configured_url()
        self._vibe_floor = _configured_floor()
        self._stop_event.clear()
        self._connection_attempts = 0

        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

        # Give the handshake a moment; Intiface is local so this is fast.
        for _ in range(50):
            if self._state.connected:
                return True
            if self._stop_event.is_set():
                return False
            time.sleep(0.1)
        return self._state.connected

    def disconnect(self) -> None:
        self._stop_event.set()
        if self._loop:
            try:
                future = asyncio.run_coroutine_threadsafe(self._shutdown_async(), self._loop)
                future.result(timeout=5)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        with self._lock:
            self._devices.clear()
            self._mode = "idle"
            self._move_active = False
        self._update_state(connected=False, running=False, engineReady=False,
                           device_count=0, devices=[])
        log.info("Buttplug disconnected")

    async def _shutdown_async(self):
        try:
            await self._send({"StopAllDevices": {"Id": self._next_id()}})
        except Exception:
            pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None

    def _run_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as exc:
            log.error("Buttplug thread error: %s", exc)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None

    async def _main(self):
        while not self._stop_event.is_set():
            try:
                await self._connect_and_serve()
            except Exception as exc:
                log.warning("Buttplug connection lost: %s", exc)
            finally:
                self._ws = None
                self._update_state(connected=False, engineReady=False)

            if self._stop_event.is_set():
                break
            self._connection_attempts += 1
            delay = min(
                RECONNECT_DELAY_INITIAL * (2 ** (self._connection_attempts - 1)),
                RECONNECT_DELAY_MAX,
            )
            log.info("Buttplug reconnecting in %.1fs (attempt %d)",
                     delay, self._connection_attempts)
            await asyncio.sleep(delay)

    async def _connect_and_serve(self):
        url = self._ws_url or DEFAULT_WS_URL
        log.info("Buttplug connecting to %s", url)
        async with websockets.connect(url, close_timeout=2) as ws:
            self._ws = ws
            await self._handshake()
            self._connection_attempts = 0
            self._update_state(connected=True, engineReady=True)
            log.info("Buttplug connected to Intiface at %s", url)

            tasks = [
                asyncio.create_task(self._recv_loop(ws)),
                asyncio.create_task(self._tick_loop()),
            ]
            if self._max_ping_ms > 0:
                tasks.append(asyncio.create_task(self._ping_loop()))

            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    # ── Protocol ──────────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        with self._lock:
            self._msg_id += 1
            return self._msg_id

    async def _send(self, message: dict) -> None:
        """Buttplug frames are arrays of message objects."""
        ws = self._ws
        if ws is None:
            return
        await ws.send(json.dumps([message]))

    async def _handshake(self):
        await self._send({"RequestServerInfo": {
            "Id": self._next_id(),
            "ClientName": CLIENT_NAME,
            "MessageVersion": MESSAGE_VERSION,
        }})
        # ServerInfo lands in _recv_loop; ask for the inventory straight away so
        # already-paired toys show up without waiting for a scan.
        await self._send({"RequestDeviceList": {"Id": self._next_id()}})
        await self._send({"StartScanning": {"Id": self._next_id()}})

    async def _ping_loop(self):
        interval = max(0.25, (self._max_ping_ms / 1000.0) / 2.0)
        while not self._stop_event.is_set():
            try:
                await self._send({"Ping": {"Id": self._next_id()}})
            except Exception:
                break
            await asyncio.sleep(interval)

    async def _recv_loop(self, ws):
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, list):
                    frame = [frame]
                for message in frame:
                    for name, payload in message.items():
                        self._handle_message(name, payload or {})
        except Exception as exc:
            log.warning("Buttplug recv ended: %s", exc)

    def _handle_message(self, name: str, payload: dict) -> None:
        if name == "ServerInfo":
            self._max_ping_ms = int(payload.get("MaxPingTime", 0) or 0)
            log.info("Intiface server: %s (ping %dms)",
                     payload.get("ServerName", "?"), self._max_ping_ms)

        elif name == "DeviceList":
            for entry in payload.get("Devices", []) or []:
                self._add_device(entry)
            self._publish_devices()

        elif name == "DeviceAdded":
            self._add_device(payload)
            self._publish_devices()
            log.info("Buttplug device added: %s", payload.get("DeviceName"))

        elif name == "DeviceRemoved":
            idx = payload.get("DeviceIndex")
            with self._lock:
                self._devices.pop(idx, None)
                self._selected.discard(idx)
                self._last_scalar.pop(idx, None)
            self._publish_devices()
            log.info("Buttplug device removed: index %s", idx)

        elif name == "Error":
            log.warning("Buttplug error: %s (code %s)",
                        payload.get("ErrorMessage"), payload.get("ErrorCode"))

    def _add_device(self, entry: dict) -> None:
        idx = entry.get("DeviceIndex")
        if idx is None:
            return
        messages = entry.get("DeviceMessages", {}) or {}

        scalars = []
        for i, feature in enumerate(messages.get("ScalarCmd", []) or []):
            scalars.append({
                "index": i,
                "actuator": feature.get("ActuatorType", "Vibrate"),
                "steps": feature.get("StepCount", 0),
            })
        linears = [
            {"index": i, "steps": f.get("StepCount", 0)}
            for i, f in enumerate(messages.get("LinearCmd", []) or [])
        ]
        rotates = [
            {"index": i, "steps": f.get("StepCount", 0)}
            for i, f in enumerate(messages.get("RotateCmd", []) or [])
        ]

        with self._lock:
            self._devices[idx] = {
                "index": idx,
                "name": entry.get("DeviceName", f"Device {idx}"),
                "scalars": scalars,
                "linears": linears,
                "rotates": rotates,
            }

    def _publish_devices(self) -> None:
        with self._lock:
            listing = [
                {
                    "index": d["index"],
                    "name": d["name"],
                    "linear": bool(d["linears"]),
                    "scalar": bool(d["scalars"]),
                    "actuators": [s["actuator"] for s in d["scalars"]],
                }
                for d in self._devices.values()
            ]
        self._update_state(device_count=len(listing), devices=listing)

    def _driven(self) -> List[dict]:
        """The toys we should be commanding right now."""
        with self._lock:
            if not self._selected:
                return list(self._devices.values())
            return [d for i, d in self._devices.items() if i in self._selected]

    # ── Ticker ────────────────────────────────────────────────────────────────

    async def _tick_loop(self):
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as exc:
                log.debug("Buttplug tick error: %s", exc)
            await asyncio.sleep(TICK_INTERVAL)

    async def _tick(self):
        now = time.monotonic()
        start_linear = False

        with self._lock:
            # Advance the current move.
            if self._move_active:
                if self._move_dur <= 0:
                    self._pos = self._move_to
                    self._move_active = False
                else:
                    t = (now - self._move_start) / self._move_dur
                    if t >= 1.0:
                        self._pos = self._move_to
                        self._move_active = False
                    else:
                        self._pos = self._move_from + (self._move_to - self._move_from) * t

            # Pattern mode queues the next leg as soon as the last one lands.
            if self._mode == "pattern" and not self._move_active and self._pattern:
                move = self._pattern.next_move(self._pattern_index, self._pos)
                if move.skip:
                    # Stop'n'Go is in its pause; hold and retry next tick without
                    # burning a stroke index.
                    pass
                else:
                    self._pattern_index += 1
                    self._begin_move(move.target, move.duration_ms / 1000.0, now)
                    start_linear = True

            if self._pending_linear:
                start_linear = True
                self._pending_linear = False

            pos = self._pos
            mode = self._mode
            move_to = self._move_to
            move_dur_ms = int(self._move_dur * 1000)
            floor = self._vibe_floor
            stopped = self._state.emergency_stopped

        if stopped:
            return

        # Position -> vibration. This is the whole trick: 0 -> 0, 100 -> 1, and
        # an oscillating position becomes an oscillating vibration.
        if mode == "idle":
            scalar = 0.0
        else:
            scalar = floor + (1.0 - floor) * (max(0.0, min(100.0, pos)) / 100.0)

        await self._emit_scalars(scalar)
        if start_linear and mode != "idle":
            await self._emit_linear(move_to, move_dur_ms)
        await self._emit_rotates(scalar, pos)

        self._emit_position(pos, mode != "idle", now)

    def _begin_move(self, target: float, duration_s: float, now: float) -> None:
        """Caller must hold _lock."""
        self._move_from = self._pos
        self._move_to = max(0.0, min(100.0, target))
        self._move_dur = max(0.0, duration_s)
        self._move_start = now
        self._move_active = True

    async def _emit_scalars(self, scalar: float) -> None:
        for device in self._driven():
            subs = [
                {"Index": s["index"], "Scalar": round(scalar, 3),
                 "ActuatorType": s["actuator"]}
                for s in device["scalars"]
                if s["actuator"] in DRIVEN_SCALAR_ACTUATORS
            ]
            if not subs:
                continue

            idx = device["index"]
            previous = self._last_scalar.get(idx)
            if previous is not None:
                # An unchanged value is never worth resending — without this the
                # driver would ship 20 zero-messages/second while idle, which is
                # exactly the flood that makes BLE toys stutter.
                if scalar == previous:
                    continue
                # Below the deadband the toy can't render the difference anyway.
                # Transitions *to* zero are exempt so a stop is never swallowed.
                if scalar > 0.0 and abs(scalar - previous) < SCALAR_DEADBAND:
                    continue
            self._last_scalar[idx] = scalar

            await self._send({"ScalarCmd": {
                "Id": self._next_id(),
                "DeviceIndex": idx,
                "Scalars": subs,
            }})

    async def _emit_linear(self, target: float, duration_ms: int) -> None:
        if duration_ms <= 0:
            return
        position = round(max(0.0, min(100.0, target)) / 100.0, 3)
        for device in self._driven():
            if not device["linears"]:
                continue
            await self._send({"LinearCmd": {
                "Id": self._next_id(),
                "DeviceIndex": device["index"],
                "Vectors": [
                    {"Index": lin["index"], "Position": position,
                     "Duration": int(duration_ms)}
                    for lin in device["linears"]
                ],
            }})

    async def _emit_rotates(self, scalar: float, pos: float) -> None:
        """Rotators: treat as a vibrator that reverses on stroke direction."""
        with self._lock:
            direction = 1 if self._move_to >= self._move_from else -1
            changed = direction != self._last_direction
            self._last_direction = direction

        if not changed and scalar > 0.0:
            return
        for device in self._driven():
            if not device["rotates"]:
                continue
            await self._send({"RotateCmd": {
                "Id": self._next_id(),
                "DeviceIndex": device["index"],
                "Rotations": [
                    {"Index": rot["index"], "Speed": round(scalar, 3),
                     "Clockwise": direction > 0}
                    for rot in device["rotates"]
                ],
            }})

    def _emit_position(self, pos: float, running: bool, now: float) -> None:
        """Feed the existing device gauge, which speaks OSSM's position message."""
        if now - self._last_emit < POSITION_EMIT_INTERVAL:
            return
        self._last_emit = now
        self._state.extra["pct"] = round(pos, 1)
        self._state.extra["running"] = running
        payload = {
            "type": "position",
            "pct": round(pos, 1),
            "steps": 0,
            "running": running,
            "homed": True,
            "engineReady": self._state.connected,
        }
        for cb in list(self._listeners):
            try:
                cb(payload)
            except Exception:
                pass

    # ── AbstractDevice API ────────────────────────────────────────────────────

    def send_command(self, command: Dict[str, Any]) -> None:
        """Accepts the OSSM command vocabulary plus a few Buttplug extras.

        Never blocks: everything mutates state that the ticker picks up, except
        the immediate stop paths which are scheduled onto the asyncio loop.
        """
        cmd = command.get("cmd")

        if cmd == "stream":
            # Funscript player: absolute target with an explicit travel time.
            pct = float(command.get("pct", 0))
            duration = float(command.get("duration", 100)) / 1000.0
            with self._lock:
                self._mode = "stream"
                self._state.emergency_stopped = False
                self._begin_move(pct, duration, time.monotonic())
                self._pending_linear = True

        elif cmd == "moveTo":
            pct = float(command.get("pct", 50))
            speed_pct = float(command.get("speedPct", 50))
            with self._lock:
                distance = abs(pct - self._pos)
                # Rough but predictable: full travel at 100% takes ~250ms.
                rate = max(10.0, speed_pct * 4.0)
                self._mode = "stream"
                self._state.emergency_stopped = False
                self._begin_move(pct, distance / rate, time.monotonic())
                self._pending_linear = True

        elif cmd in ("stop", "stopPattern"):
            self._go_idle()

        elif cmd == "setSpeedPct":
            with self._lock:
                self._speed = float(command.get("value", self._speed))
                self._refresh_pattern_params()

        elif cmd == "setDepthPct":
            with self._lock:
                self._depth = float(command.get("value", self._depth))
                self._refresh_pattern_params()

        elif cmd == "setStrokePct":
            # AIMO derives stroke from depth-base; invert to keep base in sync.
            with self._lock:
                stroke = float(command.get("value", 0))
                self._base = max(0.0, self._depth - stroke)
                self._refresh_pattern_params()

        elif cmd == "setSensation":
            with self._lock:
                self._sensation = float(command.get("value", 0))
                self._refresh_pattern_params()

        elif cmd == "startPattern":
            with self._lock:
                if self._pattern is None:
                    self._pattern = make_pattern("simple_stroke")
                    self._refresh_pattern_params()
                self._mode = "pattern"
                self._state.emergency_stopped = False

        # ── Buttplug-specific ────────────────────────────────────────────────

        elif cmd == "select_devices":
            indices = command.get("indices") or []
            with self._lock:
                self._selected = {int(i) for i in indices}
            log.info("Buttplug driving devices: %s", self._selected or "all")

        elif cmd == "scan":
            self._schedule(self._send({"StartScanning": {"Id": self._next_id()}}))

        elif cmd == "set_vibe_floor":
            with self._lock:
                self._vibe_floor = max(0.0, min(0.9, float(command.get("value", 0.0))))

    def apply_ai_commands(self, commands: dict) -> None:
        """Drive from the orchestrator's compiled intent."""
        if not self._state.connected:
            return

        pattern = commands.get("pattern")
        speed = commands.get("speed")
        depth = commands.get("depth")
        base = commands.get("base")
        intensity = commands.get("intensity")

        with self._lock:
            if speed is not None:
                self._speed = float(speed)
            if depth is not None:
                self._depth = float(depth)
            if base is not None:
                self._base = float(base)
            if intensity is not None:
                self._sensation = float(intensity)

            if pattern == "stop":
                self._pattern = None
            elif pattern is not None:
                new_pattern = make_pattern(pattern)
                if new_pattern is None:
                    log.warning("Unknown pattern %r; falling back to simple_stroke", pattern)
                    new_pattern = make_pattern("simple_stroke")
                # Swapping mid-session restarts the stroke index so ramping
                # patterns (Deeper, Stop'n'Go) begin from their first stroke.
                self._pattern = new_pattern
                self._pattern_index = 0

            self._refresh_pattern_params()
            has_pattern = self._pattern is not None

        if pattern == "stop" or not has_pattern:
            self._go_idle()
        else:
            with self._lock:
                self._mode = "pattern"
                self._state.emergency_stopped = False

        # Manual moveTo passthrough, matching OSSM's handling.
        for manual in commands.get("commands") or []:
            if manual.get("name") == "moveTo":
                args = manual.get("args", {})
                self.send_command({
                    "cmd": "moveTo",
                    "pct": args.get("pos", 50),
                    "speedPct": args.get("speed", 50),
                })

    def _refresh_pattern_params(self) -> None:
        """Caller must hold _lock."""
        if self._pattern is not None:
            self._pattern.set_params(self._speed, self._depth, self._base, self._sensation)

    def _go_idle(self) -> None:
        with self._lock:
            self._mode = "idle"
            self._move_active = False
            self._pos = 0.0
        self._schedule(self._stop_all())
        self._update_state(running=False)

    def emergency_stop(self) -> None:
        with self._lock:
            self._mode = "idle"
            self._move_active = False
            self._pattern = None
            self._pos = 0.0
            self._state.emergency_stopped = True
            self._last_scalar.clear()
        self._schedule(self._stop_all())
        self._update_state(emergency_stopped=True, running=False, engineReady=False)
        log.warning("BUTTPLUG EMERGENCY STOP")

    async def _stop_all(self) -> None:
        try:
            await self._send({"StopAllDevices": {"Id": self._next_id()}})
        except Exception as exc:
            log.debug("Buttplug stop-all failed: %s", exc)

    def _schedule(self, coro) -> None:
        """Run a coroutine on the device loop from a Flask thread."""
        loop = self._loop
        if loop is None or self._ws is None:
            coro.close()
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()

    # ── Introspection for the setup UI ────────────────────────────────────────

    def list_devices(self) -> List[dict]:
        with self._lock:
            return [
                {
                    "index": d["index"],
                    "name": d["name"],
                    "linear": bool(d["linears"]),
                    "scalar": bool(d["scalars"]),
                    "actuators": [s["actuator"] for s in d["scalars"]],
                    "selected": (not self._selected) or (d["index"] in self._selected),
                }
                for d in self._devices.values()
            ]
