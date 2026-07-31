"""
devices/ossm_ble.py
-------------------
Stock KinkyMakers OSSM firmware (v1.0.x) over BLE.

Why this is a second OSSM driver
--------------------------------
devices/ossm.py speaks the custom firmware's JSON-over-WebSocket/serial
protocol. The stock firmware has no serial control path at all — its only
command surface is the NimBLE GATT service — and the vocabulary is a different
shape: bare ASCII strings, strictly integer 0-100 arguments, and a mode entry
that *resets* every motion setting on the way in.

Pattern/AI mode only. The stock firmware's streaming mode blocks on every
direction reversal, so the funscript path stays on the custom firmware and a
`stream`/`moveTo` here is dropped rather than approximated badly.

Reconciliation, not sequencing
------------------------------
Entering strokeEngine runs resetSettingsStrokeEngine() on the device, which
puts speed/stroke/depth/sensation back to firmware defaults. Rather than trying
to order writes around that, this driver holds a *desired* settings dict and
re-sends whatever the device has drifted away from. Entering the mode, or a new
sessionId, just clears the applied set and everything is pushed again — which
also makes reconnects and mid-session menu trips self-healing.

Position is simulated
---------------------
The firmware notifies only when its state *fingerprint* changes, and position
is not part of that fingerprint — so there is no live position feed to drive a
gauge from. Instead the same seven patterns the firmware runs are reproduced
host-side by devices/stroke_patterns.py, driven from the settings the firmware
*reports* (i.e. post speed-knob), and the needle follows that. It mirrors what
the machine has been told to do; it is not a measurement of where it is.
"""

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover - bleak is a hard dep, but stay importable
    BleakClient = None  # type: ignore
    BleakScanner = None  # type: ignore

from config import AI_TO_DEVICE_PATTERN_MAP
from .base import AbstractDevice
from .stroke_patterns import make_pattern

log = logging.getLogger(__name__)

# GATT layout, from Software/src/services/communication/nimble.h.
SERVICE_UUID = "522b443a-4f53-534d-0001-420badbabe69"
COMMAND_CHAR = "522b443a-4f53-534d-1000-420badbabe69"
SPEED_KNOB_CHAR = "522b443a-4f53-534d-1010-420badbabe69"
STATE_CHAR = "522b443a-4f53-534d-2000-420badbabe69"

# Advertised name defaults to "OSSM" but is user-renameable (8 chars), so the
# scan matches on the service UUID too.
DEVICE_NAME_HINT = "ossm"

CONNECT_TIMEOUT = 12.0
RECONNECT_DELAY_INITIAL = 1.0
RECONNECT_DELAY_MAX = 30.0

WRITER_INTERVAL = 0.05
# The device runs std::regex over every write and drains its queue one command
# per FreeRTOS tick, so writes are spaced rather than blasted.
WRITE_GAP = 0.01
MODE_REQUEST_INTERVAL = 1.5

TICK_HZ = 20.0
TICK_INTERVAL = 1.0 / TICK_HZ
POSITION_EMIT_INTERVAL = 0.1

# What resetSettingsStrokeEngine() leaves behind on the device (actions.cpp).
STROKE_ENGINE_DEFAULTS = {"speed": 0, "stroke": 50, "depth": 10, "sensation": 50}

# Pattern and geometry land before speed, so the carriage never starts stroking
# at a stale depth — the same ordering trap the custom firmware driver hits.
SETTING_ORDER = ("pattern", "depth", "stroke", "sensation", "speed")

PATTERN_COUNT = 7

# States the machine passes through before it has a valid zero.
_UNHOMED_PREFIXES = ("idle", "homing", "hello", "error")

# strokeEngine.preflight is deliberately excluded from "in the engine": it is
# the gate waiting for the speed knob to be zeroed and the encoder pressed, and
# completing it runs resetSettingsStrokeEngine(), so anything written during
# preflight is thrown away.
_STROKE_ENGINE_PREFIX = "strokeEngine"
_STROKE_ENGINE_PREFLIGHT = "strokeEngine.preflight"

# Play modes we have to leave before strokeEngine can be entered.
_OTHER_PLAY_MODES = ("simplePenetration", "streaming")

_INDEX_TO_PATTERN = {
    index: name for name, index in AI_TO_DEVICE_PATTERN_MAP.items() if index >= 0
}


def clamp_pct(value: Any) -> int:
    """Coerce to the integer 0-100 the firmware's parser will accept.

    commands.hpp rejects anything where `valueStr != String(value)`, so floats
    and padded numbers are silently ignored by the device — they have to be
    normalised here or the command is a no-op.
    """
    try:
        result = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, result))


def sensation_to_wire(intensity: Any) -> int:
    """StrokeEngine sensation (-100..100) -> the firmware's 0..100 setting.

    AIMO's intent files emit sensation in StrokeEngine's native signed range
    (see the `intensity` fields in intents/*.json). The stock firmware takes an
    unsigned percentage and re-expands it with calculateSensation(), so a raw
    negative would be rejected outright by the 0-100 validator.
    """
    try:
        value = float(intensity)
    except (TypeError, ValueError):
        value = 0.0
    value = max(-100.0, min(100.0, value))
    return clamp_pct((value + 100.0) / 2.0)


def sensation_from_wire(value: Any) -> float:
    """Inverse of the above — mirrors utils/StrokeEngineHelper.h."""
    return clamp_pct(value) * 2.0 - 100.0


def is_homed_state(fw_state: str) -> bool:
    return bool(fw_state) and not fw_state.startswith(_UNHOMED_PREFIXES)


def is_in_stroke_engine(fw_state: str) -> bool:
    """True only where the device will hold settings we write."""
    return (fw_state.startswith(_STROKE_ENGINE_PREFIX)
            and not fw_state.startswith(_STROKE_ENGINE_PREFLIGHT))


class OSSMBleDevice(AbstractDevice):
    name = "OSSM (stock firmware, BLE)"
    device_type = "ossm_ble"

    def __init__(self, speed_knob_as_limit: bool = True):
        super().__init__()
        self._address: Optional[str] = None
        self._client: Optional[Any] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

        # True keeps the physical speed knob as a hard cap on BLE speed, which
        # is the firmware default and a genuine safety interlock. False hands
        # AIMO full authority over speed.
        self._speed_knob_as_limit = speed_knob_as_limit

        # Desired vs applied: the whole reconciliation story.
        self._desired: Dict[str, int] = dict(STROKE_ENGINE_DEFAULTS)
        self._desired["pattern"] = 0
        self._applied: Dict[str, int] = {}
        self._want_running = False
        self._exit_requested = False
        self._last_mode_request = float("-inf")
        self._outbox: deque = deque()

        # AI sends depth and base; the firmware wants depth and stroke.
        self._last_depth = 50
        self._last_base = 0

        self._fw_state = ""
        self._session_id = ""

        # Simulated position.
        self._pattern = None
        self._pattern_name: Optional[str] = None
        self._pattern_index = 0
        self._sim_speed = 0.0
        self._sim_depth = 0.0
        self._sim_base = 0.0
        self._sim_sensation = 0.0
        self._sim_running = False
        self._pos = 0.0
        self._move_from = 0.0
        self._move_to = 0.0
        self._move_dur = 0.0
        self._move_start = 0.0
        self._move_active = False
        self._last_emit = 0.0

        self._update_state(
            pct=0.0, steps=0, running=False, homed=False, engineReady=False,
            fw_state="", in_stroke_engine=False,
        )

        if BleakClient is None:
            log.error("bleak is not installed. OSSM BLE will not work.")

    # ── Backward-compatible alias ─────────────────────────────────────────────

    def send(self, cmd: dict) -> None:
        self.send_command(cmd)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self, address: Optional[str] = None) -> bool:
        if not BleakClient:
            log.error("bleak not installed")
            return False

        if self._thread and self._thread.is_alive():
            self.disconnect()

        self._address = (address or "").strip() or None
        if not self._address:
            log.error("OSSM BLE needs a device address; run a scan first")
            return False

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._ble_thread, daemon=True)
        self._thread.start()

        deadline = time.monotonic() + CONNECT_TIMEOUT
        while time.monotonic() < deadline:
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
                future = asyncio.run_coroutine_threadsafe(
                    self._disconnect_async(), self._loop)
                future.result(timeout=5)
            except Exception:
                pass

        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

        self._update_state(connected=False, engineReady=False,
                           in_stroke_engine=False, running=False)
        log.info("OSSM BLE disconnected")

    async def _disconnect_async(self) -> None:
        client = self._client
        if client is not None and getattr(client, "is_connected", False):
            try:
                await client.disconnect()
            except Exception as exc:
                log.warning("OSSM BLE disconnect error: %s", exc)
        self._client = None

    def _ble_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ble_main())
        except Exception as exc:
            log.error("OSSM BLE thread error: %s", exc)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None

    async def _ble_main(self) -> None:
        delay = RECONNECT_DELAY_INITIAL
        while not self._stop_event.is_set():
            try:
                log.info("OSSM BLE connecting to %s", self._address)
                async with BleakClient(self._address) as client:
                    self._client = client
                    self._reset_link_state()
                    self._update_state(connected=True)
                    log.info("OSSM BLE connected")
                    delay = RECONNECT_DELAY_INITIAL

                    await client.start_notify(STATE_CHAR, self._on_state_notify)
                    await self._push_speed_knob_config(client)

                    writer = asyncio.create_task(self._writer_loop(client))
                    ticker = asyncio.create_task(self._tick_loop())
                    try:
                        await self._keep_alive(client)
                    finally:
                        writer.cancel()
                        ticker.cancel()
                        await asyncio.gather(writer, ticker,
                                             return_exceptions=True)
            except Exception as exc:
                log.warning("OSSM BLE connection lost: %s", exc)
            finally:
                self._client = None
                self._update_state(connected=False, engineReady=False,
                                   in_stroke_engine=False)

            if self._stop_event.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)

    async def _keep_alive(self, client) -> None:
        while not self._stop_event.is_set():
            if not getattr(client, "is_connected", False):
                break
            await asyncio.sleep(0.5)

    def _reset_link_state(self) -> None:
        """A new link knows nothing about what the device currently holds."""
        with self._lock:
            self._applied.clear()
            self._outbox.clear()
            self._fw_state = ""
            self._session_id = ""
            self._exit_requested = False
            self._last_mode_request = float("-inf")

    async def _push_speed_knob_config(self, client) -> None:
        value = b"true" if self._speed_knob_as_limit else b"false"
        try:
            await client.write_gatt_char(SPEED_KNOB_CHAR, value, response=False)
        except Exception as exc:
            # Older builds may not expose it; the default (knob as limit) is the
            # safe one, so this is not worth failing the connection over.
            log.debug("OSSM BLE speed-knob config write skipped: %s", exc)

    # ── Writer ────────────────────────────────────────────────────────────────

    async def _writer_loop(self, client) -> None:
        while not self._stop_event.is_set():
            for command in self._drain_plan():
                try:
                    await client.write_gatt_char(
                        COMMAND_CHAR, command.encode("utf-8"), response=False)
                    log.debug("OSSM BLE << %s", command)
                except Exception as exc:
                    log.warning("OSSM BLE write failed (%s): %s", command, exc)
                    # A GATT write that fails means the link is unusable. Drop
                    # it so the reconnect loop rebuilds it, rather than holding
                    # a connection open that silently swallows every command.
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    return
                await asyncio.sleep(WRITE_GAP)
            await asyncio.sleep(WRITER_INTERVAL)

    def _drain_plan(self) -> List[str]:
        with self._lock:
            pending = list(self._outbox)
            self._outbox.clear()
            pending.extend(self._plan(time.monotonic()))
            return pending

    def _plan(self, now: float) -> List[str]:
        """Commands that would bring the device in line with what we want.

        Caller must hold the lock. Kept free of I/O so the ordering rules can be
        tested without a BLE stack.
        """
        commands: List[str] = []
        in_engine = is_in_stroke_engine(self._fw_state)

        if not in_engine:
            self._exit_requested = False

        if self._want_running:
            if not in_engine:
                if now - self._last_mode_request < MODE_REQUEST_INTERVAL:
                    return commands
                # menu.idle is the only state where the ButtonPress behind
                # go:strokeEngine means "enter the mode". From homing or
                # preflight the device arrives on its own, and an extra press
                # there would cycle the play control instead.
                if self._fw_state.startswith("menu"):
                    self._last_mode_request = now
                    commands.append("go:strokeEngine")
                elif self._fw_state.startswith(_OTHER_PLAY_MODES):
                    # Left in another play mode on the dial. Step back to the
                    # menu so the next pass can enter strokeEngine; without this
                    # an AI session would sit doing nothing.
                    self._last_mode_request = now
                    commands.append("go:menu")
                return commands

            for key in SETTING_ORDER:
                value = self._desired[key]
                if self._applied.get(key) != value:
                    commands.append(f"set:{key}:{value}")
                    self._applied[key] = value
            return commands

        if in_engine and not self._exit_requested:
            self._exit_requested = True
            self._applied.clear()
            commands.extend(("set:speed:0", "go:menu"))
        return commands

    # ── State notifications ───────────────────────────────────────────────────

    def _on_state_notify(self, _sender: Any, data: bytearray) -> None:
        try:
            payload = json.loads(bytes(data).decode("utf-8", "ignore"))
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(payload, dict):
            self.ingest_state(payload)

    def ingest_state(self, payload: Dict[str, Any]) -> None:
        """Apply one getCurrentState() payload from the device."""
        fw_state = str(payload.get("state", "") or "")
        session = str(payload.get("sessionId", "") or "")

        speed = clamp_pct(payload.get("speed", 0))
        depth = clamp_pct(payload.get("depth", 0))
        stroke = clamp_pct(payload.get("stroke", 0))
        sensation = clamp_pct(payload.get("sensation", 50))
        try:
            pattern_index = int(payload.get("pattern", 0) or 0) % PATTERN_COUNT
        except (TypeError, ValueError):
            pattern_index = 0

        with self._lock:
            was_engine = is_in_stroke_engine(self._fw_state)
            in_engine = is_in_stroke_engine(fw_state)

            # Arriving in strokeEngine means resetSettingsStrokeEngine() has
            # just wiped the device's settings, so everything we believed was
            # applied is stale. A new sessionId means the same thing for the
            # cases where we never saw the intermediate state.
            if (in_engine and not was_engine) or session != self._session_id:
                self._applied.clear()

            self._fw_state = fw_state
            self._session_id = session
            if not in_engine:
                self._exit_requested = False

            self._sync_simulation(speed, depth, stroke, sensation,
                                  pattern_index, in_engine)

        homed = is_homed_state(fw_state)
        self._update_state(
            fw_state=fw_state,
            in_stroke_engine=in_engine,
            homed=homed,
            # The stock firmware has no separate "engine ready" signal; once it
            # is homed and linked it will accept a mode change, which is what
            # the UI gate actually cares about.
            engineReady=bool(self._state.connected and homed),
            fw_speed=speed,
            fw_depth=depth,
            fw_stroke=stroke,
            fw_sensation=sensation,
            fw_pattern=pattern_index,
            position_mm=payload.get("position"),
        )

    def _sync_simulation(self, speed: int, depth: int, stroke: int,
                         sensation: int, pattern_index: int,
                         in_engine: bool) -> None:
        """Point the host-side pattern engine at what the device reports.

        Driving from the reported values rather than the commanded ones is what
        makes the needle honest about the speed knob: settings.speed is already
        the post-knob figure by the time it reaches us.
        """
        self._sim_speed = float(speed)
        self._sim_depth = float(depth)
        self._sim_base = float(max(0, depth - stroke))
        self._sim_sensation = sensation_from_wire(sensation)

        name = _INDEX_TO_PATTERN.get(pattern_index)
        if name != self._pattern_name:
            self._pattern_name = name
            self._pattern = make_pattern(name) if name else None
            self._pattern_index = 0

        if self._pattern:
            self._pattern.set_params(self._sim_speed, self._sim_depth,
                                     self._sim_base, self._sim_sensation)

        was_running = self._sim_running
        self._sim_running = bool(in_engine and speed > 0)
        if was_running and not self._sim_running:
            # Leaving the mode fires emergencyStop on the device and speed 0
            # simply stops issuing moves, so the carriage stays where it is —
            # it does not coast on to the target of the move in flight.
            self._move_active = False

    # ── Simulated position ────────────────────────────────────────────────────

    async def _tick_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                log.debug("OSSM BLE tick error: %s", exc)
            await asyncio.sleep(TICK_INTERVAL)

    def tick(self, now: Optional[float] = None) -> None:
        """Advance the simulated carriage one frame."""
        if now is None:
            now = time.monotonic()

        with self._lock:
            if self._move_active:
                if self._move_dur <= 0:
                    self._pos = self._move_to
                    self._move_active = False
                else:
                    progress = (now - self._move_start) / self._move_dur
                    if progress >= 1.0:
                        self._pos = self._move_to
                        self._move_active = False
                    else:
                        self._pos = (self._move_from
                                     + (self._move_to - self._move_from) * progress)

            if self._sim_running and self._pattern and not self._move_active:
                move = self._pattern.next_move(self._pattern_index, self._pos)
                if not move.skip:
                    # Stop'n'Go pauses by returning skip; hold position and
                    # retry next frame without burning a stroke index.
                    self._pattern_index += 1
                    self._begin_move(move.target, move.duration_ms / 1000.0, now)

            position = self._pos
            running = self._sim_running
            should_emit = now - self._last_emit >= POSITION_EMIT_INTERVAL
            if should_emit:
                self._last_emit = now
                self._state.extra["pct"] = round(position, 1)
                self._state.extra["running"] = running

        if should_emit:
            self._emit_position(position, running)

    def _begin_move(self, target: float, duration_s: float, now: float) -> None:
        """Caller must hold the lock."""
        self._move_from = self._pos
        self._move_to = max(0.0, min(100.0, target))
        self._move_dur = max(0.0, duration_s)
        self._move_start = now
        self._move_active = True

    def _emit_position(self, position: float, running: bool) -> None:
        payload = {
            "type": "position",
            "pct": round(position, 1),
            "steps": 0,
            "running": running,
            "homed": bool(self._state.extra.get("homed", False)),
            "engineReady": bool(self._state.extra.get("engineReady", False)),
            "simulated": True,
        }
        for callback in list(self._listeners):
            try:
                callback(payload)
            except Exception:
                pass

    # ── Commands ──────────────────────────────────────────────────────────────

    def send_command(self, command: Dict[str, Any]) -> None:
        """Accept the OSSM command vocabulary and fold it into desired state."""
        cmd = command.get("cmd")

        with self._lock:
            if cmd == "setSpeedPct":
                self._desired["speed"] = clamp_pct(command.get("value", 0))
            elif cmd == "setDepthPct":
                self._desired["depth"] = clamp_pct(command.get("value", 0))
                self._last_depth = self._desired["depth"]
            elif cmd == "setStrokePct":
                self._desired["stroke"] = clamp_pct(command.get("value", 0))
                self._last_base = max(0, self._last_depth - self._desired["stroke"])
            elif cmd == "setSensation":
                self._desired["sensation"] = sensation_to_wire(command.get("value", 0))
            elif cmd == "setPattern":
                try:
                    index = int(command.get("value", 0) or 0)
                except (TypeError, ValueError):
                    index = 0
                self._desired["pattern"] = max(0, min(PATTERN_COUNT - 1, index))
            elif cmd == "startPattern":
                self._want_running = True
                self._state.emergency_stopped = False
            elif cmd in ("stopPattern", "stop"):
                self._want_running = False
                self._desired["speed"] = 0
            elif cmd in ("stream", "moveTo"):
                log.debug("OSSM BLE drops %r: stock firmware streaming is not "
                          "supported by this driver", cmd)
            elif cmd == "setZero":
                # The stock firmware homes itself on mode entry; there is no
                # command for it and nothing to do here.
                pass
            else:
                log.debug("OSSM BLE ignoring unknown command: %s", command)

    def apply_ai_commands(self, commands: Dict[str, Any]) -> None:
        """Apply a compiled intent. Ordering is irrelevant — see module docs.

        Deliberately not gated on being connected: this only records intent,
        and holding it means a link that drops mid-session resumes where the
        session had got to instead of idling until the next AI turn.
        """
        pattern = commands.get("pattern")
        speed = commands.get("speed")
        depth = commands.get("depth")
        base = commands.get("base")
        intensity = commands.get("intensity")

        if commands.get("commands"):
            log.debug("OSSM BLE drops manual moveTo commands: not supported on "
                      "stock firmware")

        with self._lock:
            if speed is not None:
                self._desired["speed"] = clamp_pct(speed)
            if depth is not None:
                self._last_depth = clamp_pct(depth)
                self._desired["depth"] = self._last_depth
            if base is not None:
                self._last_base = clamp_pct(base)
            if base is not None or depth is not None:
                self._desired["stroke"] = max(0, self._last_depth - self._last_base)
            if intensity is not None:
                self._desired["sensation"] = sensation_to_wire(intensity)

            if pattern == "stop":
                self._want_running = False
                self._desired["speed"] = 0
            elif pattern is not None:
                index = AI_TO_DEVICE_PATTERN_MAP.get(pattern, 0)
                if index >= 0:
                    self._desired["pattern"] = index
                self._want_running = True
                self._state.emergency_stopped = False

    def emergency_stop(self) -> None:
        with self._lock:
            self._want_running = False
            self._desired["speed"] = 0
            self._applied.clear()
            # Jump the reconciler: these go out on the next writer pass ahead of
            # anything it would plan, and _exit_requested stops it queueing the
            # same pair again.
            self._exit_requested = True
            self._outbox.clear()
            self._outbox.extend(("set:speed:0", "go:menu"))
            self._sim_running = False
            self._move_active = False

        self._update_state(emergency_stopped=True, running=False,
                           engineReady=False)
        log.warning("OSSM BLE EMERGENCY STOP")

    # ── Scan ──────────────────────────────────────────────────────────────────

    @staticmethod
    async def _scan_async(timeout: float = 6.0) -> List[Dict[str, Any]]:
        if not BleakScanner:
            return []

        results: List[Dict[str, Any]] = []
        try:
            found = await BleakScanner.discover(timeout=timeout, return_adv=True)
            for device, adv in found.values():
                names = [device.name or "", getattr(adv, "local_name", "") or ""]
                uuids = {str(u).lower() for u in (adv.service_uuids or [])}
                if SERVICE_UUID in uuids or any(
                        DEVICE_NAME_HINT in n.lower() for n in names if n):
                    results.append({
                        "address": device.address,
                        "name": next((n for n in names if n), "OSSM"),
                        "rssi": getattr(adv, "rssi", 0),
                    })
            return results
        except TypeError:
            # bleak < 0.19 has no return_adv; fall back to name matching only.
            pass

        for device in await BleakScanner.discover(timeout=timeout):
            name = device.name or ""
            if DEVICE_NAME_HINT in name.lower():
                results.append({
                    "address": device.address,
                    "name": name,
                    "rssi": getattr(device, "rssi", 0),
                })
        return results

    @staticmethod
    def scan(timeout: float = 6.0) -> List[Dict[str, Any]]:
        """Synchronous wrapper for scan."""
        if not BleakScanner:
            return []
        try:
            return asyncio.run(OSSMBleDevice._scan_async(timeout))
        except Exception as exc:
            log.error("OSSM BLE scan error: %s", exc)
            return []
