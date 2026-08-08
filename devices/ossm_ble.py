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

Position is simulated during normal play
----------------------------------------
The firmware notifies only when its state *fingerprint* changes, and position
is not part of that fingerprint — so there is no live position feed to drive a
gauge from. Instead the same seven patterns the firmware runs are reproduced
host-side by devices/stroke_patterns.py, driven from the settings the firmware
*reports*, and the needle follows that. It mirrors what the machine has been
told to do; it is not a measurement of where it is. During explicit Stop
auto-park, however, the real firmware ``position`` field is sampled by small
depth probes and becomes the sole source of truth.

No controller assumed
---------------------
This driver targets machines with no physical remote attached, so it disables
USE_SPEED_KNOB_AS_LIMIT on connect and takes full authority over speed. On a
controller-less machine the knob pin reads at or near zero, and the firmware
default would otherwise multiply every commanded speed by it and never move.

The corollary is that there is no hardware speed cap and no physical stop: the
encoder long-press that returns the machine to the menu lives on the same
missing controller, so the app's emergency stop is the only one left.
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
# How often, at most, to re-assert speed when the device reports a value we did
# not ask for. Loose enough that the lag between a write and the device's own
# settings loop picking it up does not count as divergence.
SPEED_REASSERT_INTERVAL = 2.0

TICK_HZ = 20.0
TICK_INTERVAL = 1.0 / TICK_HZ
POSITION_EMIT_INTERVAL = 0.1

# What resetSettingsStrokeEngine() leaves behind on the device (actions.cpp).
STROKE_ENGINE_DEFAULTS = {"speed": 0, "stroke": 50, "depth": 10, "sensation": 50}

# Pattern and geometry land before speed, so the carriage never starts stroking
# at a stale depth — the same ordering trap the custom firmware driver hits.
SETTING_ORDER = ("pattern", "depth", "stroke", "sensation", "speed")

PATTERN_COUNT = 7

# Validated AIMO/Partner auto-park envelope.  The firmware's position field is
# the physical source of truth during parking; the host-side pattern simulation
# is deliberately ignored until the arm is confirmed back at the lower end.
PARK_SPEED_CAP = 8
PARK_ZONE_MIN = 0.0
PARK_ZONE_MAX = 0.5
PARK_SPEED1_STOP_POSITION = 2.5
PARK_PROBE_DEPTH_A = 25
PARK_PROBE_DEPTH_B = 24
PARK_PROBE_INTERVAL = 0.13
PARK_DESCENDING_CONFIRMATIONS = 3
# Exact main-ramp constants ported from Partner beta14a auto-park.js.
# The old final micro-recovery (depth 18/16) is intentionally NOT ported.
PARK_RAMP_TARGET_SPEED = 1
PARK_RAMP_TARGET_POSITION = 12.0
PARK_FINAL_APPROACH_POSITION = 18.0
PARK_FINAL_APPROACH_SPEED = 1
PARK_RAMP_LATENCY_COMPENSATION_POSITION = 65.0
PARK_RAMP_INTERVAL = 0.09
PARK_POSITION_HIGH = 153.0
PARK_START_SETTLE = 0.15
PARK_STATE_TIMEOUT = 1.5
PARK_TOTAL_TIMEOUT = 30.0

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

    def __init__(self):
        super().__init__()
        self._address: Optional[str] = None
        self._client: Optional[Any] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

        # Desired vs applied: the whole reconciliation story.
        self._desired: Dict[str, int] = dict(STROKE_ENGINE_DEFAULTS)
        self._desired["pattern"] = 0
        self._applied: Dict[str, int] = {}
        self._want_running = False
        # Narrative/user Pause is a speed-zero hold inside StrokeEngine.  It is
        # deliberately distinct from the explicit user Stop, which auto-parks.
        self._motion_paused = False
        self._exit_requested = False
        self._last_mode_request = float("-inf")
        self._last_speed_reassert = float("-inf")
        # Critical pause/stop transitions invalidate command batches already
        # drained by the asynchronous writer.
        self._command_epoch = 0
        self._outbox: deque = deque()

        # AI sends depth and base; the firmware wants depth and stroke.
        self._last_depth = 50
        self._last_base = 0

        self._fw_state = ""
        self._session_id = ""
        self._fw_timestamp: Optional[Any] = None
        self._fw_position: Optional[float] = None
        self._fw_speed = 0

        # Explicit user Stop auto-park state. Narrative/user Pause uses a separate
        # speed-zero hold and never enters this state machine.
        self._parking = False
        self._park_mode = ""
        self._park_phase = ""
        self._park_started_at = 0.0
        self._park_phase_started_at = 0.0
        self._park_last_probe_at = 0.0
        self._park_last_timestamp: Optional[Any] = None
        self._park_last_position: Optional[float] = None
        self._park_descending_count = 0
        self._park_return_confirmed = False
        self._park_return_setup_sent = False
        self._park_final_stop_armed = False
        self._park_low_before_final_logged = False
        self._park_last_ramp_speed: Optional[int] = None
        self._park_last_ramp_at = 0.0
        self._park_return_start_position: Optional[float] = None
        self._park_menu_sent = False
        self._park_probe_depth = PARK_PROBE_DEPTH_A
        self._park_entry_speed = PARK_SPEED_CAP
        self._park_saved_desired: Dict[str, int] = {}
        self._park_blocked = False

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
            fw_state="", in_stroke_engine=False, parking=False, parked=False,
            paused=False, park_mode="", park_error="", park_blocked=False,
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
        result = bool(self._state.connected)
        return result

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


                    # Partner reference sequence: subscribe first, then retry
                    # reading a usable JSON state. Keep the BLE link even when
                    # readValue temporarily exposes an old ok:<command> value.
                    await client.start_notify(STATE_CHAR, self._on_state_notify)
                    initial_payload = None
                    for attempt in range(8):
                        try:
                            raw_state = await client.read_gatt_char(STATE_CHAR)
                            raw_text = bytes(raw_state).decode("utf-8", "ignore")
                            candidate = json.loads(raw_text)
                            if (isinstance(candidate, dict)
                                    and str(candidate.get("state", "") or "").strip()):
                                initial_payload = candidate
                                break
                        except (ValueError, UnicodeDecodeError) as exc:
                            pass
                        except Exception as exc:
                            log.debug(
                                "OSSM BLE initial state read %s/8 skipped: %s",
                                attempt + 1, exc)
                        await asyncio.sleep(0.25)

                    self._update_state(connected=True)
                    if initial_payload is not None:
                        self.ingest_state(initial_payload)
                    else:
                        log.warning(
                            "OSSM BLE connected without initial JSON state; "
                            "state notifications remain active")

                    await self._disable_speed_knob_limit(client)

                    log.info("OSSM BLE connected (%s)",
                             self._fw_state or "state pending")
                    delay = RECONNECT_DELAY_INITIAL

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
            self._command_epoch += 1
            self._outbox.clear()
            self._fw_state = ""
            self._session_id = ""
            self._fw_timestamp = None
            self._fw_position = None
            self._fw_speed = 0
            self._exit_requested = False
            self._last_mode_request = float("-inf")
            self._last_speed_reassert = float("-inf")
            self._parking = False
            self._park_mode = ""
            self._park_phase = ""
            self._park_saved_desired.clear()
            self._park_blocked = False
            self._motion_paused = False

    async def _disable_speed_knob_limit(self, client) -> None:
        """Take the physical speed knob out of the speed calculation.

        The firmware defaults USE_SPEED_KNOB_AS_LIMIT to true, which computes
        speed as `knob * bleSpeed / 100`. On a machine with no controller
        attached the knob pin reads at or near zero, so that multiplication
        pins speed to zero and nothing moves however fast the AI asks for. With
        it false, `set:speed:` is taken as an absolute.

        This is load-bearing rather than an optimisation, so a failure is
        reported instead of shrugged off — and read back, because the whole
        session is silently dead if it did not land.
        """
        try:
            await client.write_gatt_char(SPEED_KNOB_CHAR, b"false", response=True)
        except Exception as exc:
            log.error("OSSM BLE could not disable the speed-knob limit (%s); "
                      "the machine will not move without a controller", exc)
            self._update_state(speed_knob_limit_disabled=False)
            return

        confirmed = False
        try:
            readback = bytes(await client.read_gatt_char(SPEED_KNOB_CHAR))
            confirmed = readback.strip().lower() == b"false"
            if not confirmed:
                log.error("OSSM BLE speed-knob limit still reads %r; firmware "
                          "may predate the config characteristic", readback)
        except Exception as exc:
            # Not fatal on its own: the write may well have taken.
            log.warning("OSSM BLE could not read back the speed-knob "
                        "config: %s", exc)

        self._update_state(speed_knob_limit_disabled=confirmed)

    # ── Pause / Stop auto-park ───────────────────────────────────────────────

    def _request_motion_pause_locked(self, source: str) -> None:
        """Hold speed at zero without parking or leaving StrokeEngine.

        Used by narrative AI ``pattern=stop`` and the user Pause button.  The
        desired geometry and speed are preserved for the next explicit resume.
        Caller holds ``_lock``.
        """
        if self._parking:
            return

        self._motion_paused = True
        self._command_epoch += 1
        self._outbox.clear()
        self._outbox.append("set:speed:0")
        # The zero is queued explicitly; keep the reconciler from immediately
        # restoring the saved desired speed while the pause remains active.
        self._applied["speed"] = 0
        self._sim_running = False
        self._move_active = False
        self._update_state(running=False, paused=True, parked=False)

    def _resume_motion_locked(self, source: str) -> None:
        """Release a speed-zero hold; settings are reconciled speed-last."""
        if not self._motion_paused:
            return
        self._motion_paused = False
        self._command_epoch += 1
        # Remove a not-yet-drained pause zero and force a complete safe replay.
        self._outbox.clear()
        self._applied.clear()
        self._exit_requested = False
        self._update_state(paused=False, parked=False, park_error="")

    @staticmethod
    def _coerce_position(value: Any) -> Optional[float]:
        try:
            position = float(value)
        except (TypeError, ValueError):
            return None
        return position if position == position else None

    @staticmethod
    def _is_park_position(position: Optional[float]) -> bool:
        return (position is not None
                and PARK_ZONE_MIN <= position <= PARK_ZONE_MAX)

    def _request_park_locked(self, mode: str) -> None:
        """Begin one automatic lower-end park.  Caller holds ``_lock``."""
        if self._parking:
            return

        now = time.monotonic()
        self._parking = True
        self._motion_paused = False
        self._command_epoch += 1
        self._park_mode = "stop" if mode == "stop" else "pause"
        self._park_phase = "stopping"
        self._park_started_at = now
        self._park_phase_started_at = now
        self._park_last_probe_at = float("-inf")
        self._park_last_timestamp = self._fw_timestamp
        self._park_last_position = self._fw_position
        self._park_descending_count = 0
        self._park_return_confirmed = False
        self._park_return_setup_sent = False
        self._park_final_stop_armed = False
        self._park_low_before_final_logged = False
        self._park_last_ramp_speed = None
        self._park_last_ramp_at = 0.0
        self._park_return_start_position = None
        self._park_menu_sent = False
        self._park_probe_depth = PARK_PROBE_DEPTH_A
        self._park_saved_desired = dict(self._desired)
        self._park_blocked = False

        reported = clamp_pct(self._fw_speed)
        requested = clamp_pct(self._desired.get("speed", 0))
        source_speed = reported if reported > 0 else requested
        self._park_entry_speed = max(1, min(PARK_SPEED_CAP, source_speed or PARK_SPEED_CAP))
        self._park_last_ramp_speed = self._park_entry_speed

        self._want_running = False
        self._exit_requested = True
        self._applied.clear()
        self._outbox.clear()
        # First action is always an immediate speed zero.  The controlled park
        # starts only after the real firmware position has been considered.
        self._outbox.append("set:speed:0")
        self._sim_running = False
        self._move_active = False
        self._update_state(
            parking=True, parked=False, park_mode=self._park_mode,
            park_error="", park_blocked=False, running=False, engineReady=False,
        )

    def _abort_park_locked(self, to_menu: bool, reason: str) -> None:
        """Second stop / emergency path: zero immediately, no more parking."""
        self._parking = False
        self._motion_paused = False
        self._command_epoch += 1
        self._park_phase = ""
        self._park_blocked = True
        self._want_running = False
        self._outbox.clear()
        self._outbox.append("set:speed:0")
        if to_menu:
            self._outbox.append("go:menu")
        self._applied.clear()
        self._exit_requested = True
        self._sim_running = False
        self._move_active = False
        self._update_state(
            parking=False, parked=False, park_mode="",
            park_error=reason, park_blocked=True,
            running=False, engineReady=False,
        )

    def _complete_park_locked(self) -> None:
        """Adopt the confirmed lower state and release Pause/Stop."""
        mode = self._park_mode
        saved = dict(self._park_saved_desired or self._desired)
        self._parking = False
        self._motion_paused = False
        self._park_mode = ""
        self._park_phase = ""
        self._park_saved_desired.clear()
        self._park_blocked = False
        self._want_running = False
        # Keep a successful Pause inside strokeEngine at speed zero.  Stop has
        # already reached menu, where this flag is harmless.
        self._exit_requested = True
        self._applied.clear()
        # Keep the GUI/session values for a later resume, but never command them
        # until a fresh explicit Start/Resume occurs.
        self._desired.update(saved)
        self._sim_running = False
        self._move_active = False
        self._pos = 0.0
        self._move_from = 0.0
        self._move_to = 0.0
        self._update_state(
            pct=0.0, running=False, parking=False, parked=True,
            park_mode="", park_error="", park_blocked=False,
            engineReady=bool(self._state.connected and is_homed_state(self._fw_state)),
            position_mm=self._fw_position,
        )

    def _compute_park_ramp_speed(self, position: Optional[float]) -> int:
        """Exact Partner beta14a proportional ramp, translated to Python."""
        entry_speed = clamp_pct(self._park_entry_speed)
        if position is None:
            return entry_speed
        if position <= PARK_ZONE_MAX:
            return 0
        if position <= PARK_FINAL_APPROACH_POSITION:
            return PARK_FINAL_APPROACH_SPEED

        max_speed = max(PARK_RAMP_TARGET_SPEED, entry_speed)
        effective_position = max(
            PARK_RAMP_TARGET_POSITION,
            position - PARK_RAMP_LATENCY_COMPENSATION_POSITION,
        )
        if effective_position <= PARK_RAMP_TARGET_POSITION:
            return PARK_RAMP_TARGET_SPEED

        effective_high = max(
            PARK_RAMP_TARGET_POSITION + 1.0,
            PARK_POSITION_HIGH - PARK_RAMP_LATENCY_COMPENSATION_POSITION,
        )
        ratio = max(0.0, min(
            1.0,
            (effective_position - PARK_RAMP_TARGET_POSITION)
            / (effective_high - PARK_RAMP_TARGET_POSITION),
        ))
        raw_speed = (
            PARK_RAMP_TARGET_SPEED
            + (max_speed - PARK_RAMP_TARGET_SPEED) * ratio
        )
        # JavaScript Math.round() for non-negative values, not Python's
        # bankers-rounding, so the port stays byte-for-byte equivalent in its
        # speed decisions.
        speed = int(raw_speed + 0.5)
        return max(
            PARK_RAMP_TARGET_SPEED,
            min(max_speed, clamp_pct(speed)),
        )

    def _plan_park(self, now: float) -> List[str]:
        """Advance the Partner beta14a main auto-park state machine.

        The main proportional ramp is ported exactly. The abandoned final
        micro-recovery is deliberately absent: a near-low final position never
        starts another revolution.
        """
        commands: List[str] = []
        position = self._fw_position

        if now - self._park_started_at > PARK_TOTAL_TIMEOUT:
            self._abort_park_locked(False, "auto-park timeout")
            return []

        if self._park_phase == "stopping":
            if now - self._park_phase_started_at < PARK_START_SETTLE:
                return commands
            if position is None:
                if now - self._park_phase_started_at > PARK_STATE_TIMEOUT:
                    self._abort_park_locked(False, "position firmware indisponible")
                return commands

            if self._is_park_position(position):
                if self._park_mode == "stop":
                    self._park_phase = "menu_wait"
                    self._park_phase_started_at = now
                    self._park_menu_sent = True
                    commands.append("go:menu")
                else:
                    self._complete_park_locked()
                return commands

            self._park_phase = "probing"
            self._park_phase_started_at = now
            self._park_last_probe_at = now
            self._park_last_ramp_at = now
            self._park_probe_depth = PARK_PROBE_DEPTH_A
            commands.extend((
                f"set:speed:{self._park_entry_speed}",
                f"set:depth:{self._park_probe_depth}",
            ))
            return commands

        if self._park_phase == "probing":
            speed_one_low_enough = (
                self._park_final_stop_armed
                and self._fw_speed <= PARK_RAMP_TARGET_SPEED
                and position is not None
                and PARK_ZONE_MIN <= position <= PARK_SPEED1_STOP_POSITION
            )
            passed_lower_end = (
                self._park_final_stop_armed
                and self._park_return_confirmed
                and position is not None
                and position < PARK_ZONE_MIN
            )
            low_detected = self._is_park_position(position)

            if self._park_final_stop_armed and (
                    low_detected or speed_one_low_enough or passed_lower_end):
                self._park_phase = "final_wait"
                self._park_phase_started_at = now
                commands.append("set:speed:0")
                return commands

            if low_detected and not self._park_final_stop_armed:
                if not self._park_low_before_final_logged:
                    self._park_low_before_final_logged = True

            if self._park_return_confirmed and not self._park_return_setup_sent:
                self._park_return_setup_sent = True
                self._park_final_stop_armed = True
                commands.extend(("set:pattern:0", "set:stroke:100"))

            if (self._park_return_confirmed
                    and position is not None
                    and now - self._park_last_ramp_at >= PARK_RAMP_INTERVAL):
                previous_speed = (
                    self._park_last_ramp_speed
                    if self._park_last_ramp_speed is not None
                    else self._park_entry_speed
                )
                computed_speed = self._compute_park_ramp_speed(position)
                next_speed = min(previous_speed, computed_speed)
                if next_speed < previous_speed:
                    self._park_last_ramp_speed = next_speed
                    self._park_last_ramp_at = now
                    commands.append(f"set:speed:{next_speed}")

            if now - self._park_last_probe_at >= PARK_PROBE_INTERVAL:
                self._park_probe_depth = (
                    PARK_PROBE_DEPTH_B
                    if self._park_probe_depth == PARK_PROBE_DEPTH_A
                    else PARK_PROBE_DEPTH_A
                )
                self._park_last_probe_at = now
                commands.append(f"set:depth:{self._park_probe_depth}")
            return commands

        if self._park_phase == "final_wait":
            final_low = (
                self._is_park_position(position)
                or (self._park_return_confirmed
                    and position is not None
                    and position < PARK_ZONE_MIN)
            )
            if self._fw_speed == 0 and final_low:
                if self._park_mode == "stop":
                    self._park_phase = "menu_wait"
                    self._park_phase_started_at = now
                    if not self._park_menu_sent:
                        self._park_menu_sent = True
                        commands.append("go:menu")
                else:
                    self._complete_park_locked()
            return commands

        if self._park_phase == "menu_wait":
            if self._fw_state.startswith("menu"):
                self._complete_park_locked()
            elif (not self._park_menu_sent
                  or now - self._park_phase_started_at >= MODE_REQUEST_INTERVAL):
                self._park_menu_sent = True
                self._park_phase_started_at = now
                commands.append("go:menu")
            return commands

        return commands

    # ── Writer ────────────────────────────────────────────────────────────────

    async def _writer_loop(self, client) -> None:
        while not self._stop_event.is_set():
            for epoch, command in self._drain_plan():
                with self._lock:
                    if epoch != self._command_epoch:
                        continue
                try:
                    response = await self._write_command_and_wait(client, command)
                    if response.startswith("fail:"):
                        raise RuntimeError(
                            f"OSSM firmware rejected {command}: {response}")
                    log.debug("OSSM BLE << %s ; >> %s", command, response)
                except Exception as exc:
                    log.warning("OSSM BLE write failed (%s): %s", command, exc)
                    # Rebuild the link rather than retaining optimistic
                    # _applied values after an incomplete command sequence.
                    with self._lock:
                        self._applied.clear()
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    return
                await asyncio.sleep(WRITE_GAP)
            await asyncio.sleep(WRITER_INTERVAL)

    async def _write_command_and_wait(self, client, command: str) -> str:
        """Use the validated Partner transport semantics.

        One command is written with response, then the command characteristic
        is polled until it returns the command echo or an explicit
        ok:<command> / fail:<command> acknowledgement. This prevents a
        multi-command update from being blasted into the firmware queue.
        """
        payload = command.encode("utf-8")
        await client.write_gatt_char(COMMAND_CHAR, payload, response=True)

        last_text = ""
        for attempt in range(8):
            await asyncio.sleep(0.04 if attempt == 0 else 0.10)
            try:
                raw = await client.read_gatt_char(COMMAND_CHAR)
                text = bytes(raw).decode("utf-8", "ignore").replace(
                    "\x00", "").strip()
                if text:
                    last_text = text
                if text in (command, f"ok:{command}", f"fail:{command}"):
                    return text
            except Exception as exc:
                last_text = f"read-error:{exc}"

        # Partner continues after a readable but stale response; however a
        # completely unreadable characteristic is treated as a broken link.
        if not last_text or last_text.startswith("read-error:"):
            raise RuntimeError(
                f"no readable acknowledgement for {command}: "
                f"{last_text or 'empty'}")
        return last_text

    def _drain_plan(self) -> List[tuple[int, str]]:
        with self._lock:
            epoch = self._command_epoch
            pending = list(self._outbox)
            self._outbox.clear()
            pending.extend(self._plan(time.monotonic()))
            if pending:
                pass
            return [(epoch, command) for command in pending]

    def _plan(self, now: float) -> List[str]:
        """Commands that would bring the device in line with what we want.

        Caller must hold the lock. Kept free of I/O so the ordering rules can be
        tested without a BLE stack.
        """
        commands: List[str] = []
        if self._parking:
            return self._plan_park(now)

        in_engine = is_in_stroke_engine(self._fw_state)

        if not in_engine:
            self._exit_requested = False

        if self._motion_paused:
            # A pause is not a session stop: stay in StrokeEngine and hold the
            # real motor at zero until a later Start/AI pattern releases it.
            if in_engine and self._applied.get("speed") != 0:
                self._applied["speed"] = 0
                commands.append("set:speed:0")
            return commands

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
        raw_text = bytes(data).decode("utf-8", "ignore")
        try:
            payload = json.loads(raw_text)
        except (ValueError, UnicodeDecodeError) as exc:
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
        timestamp = payload.get("timestamp")
        position = self._coerce_position(payload.get("position"))
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
            self._fw_timestamp = timestamp
            self._fw_position = position
            self._fw_speed = speed
            if not in_engine and not self._parking:
                self._exit_requested = False

            # With the knob limit off, reported speed should match what we
            # asked for. If the firmware gives speed authority back to the
            # floating knob input, force a safe re-send of the requested speed.
            now = time.monotonic()
            if (in_engine and self._want_running
                    and self._applied.get("speed") == self._desired["speed"]
                    and speed != self._desired["speed"]
                    and now - self._last_speed_reassert >= SPEED_REASSERT_INTERVAL):
                self._last_speed_reassert = now
                self._applied.pop("speed", None)

            if self._parking and timestamp != self._park_last_timestamp:
                previous = self._park_last_position
                self._park_last_timestamp = timestamp
                self._park_last_position = position
                if previous is not None and position is not None:
                    if position < previous:
                        self._park_descending_count += 1
                    else:
                        self._park_descending_count = 0
                    if (not self._park_return_confirmed
                            and self._park_descending_count
                            >= PARK_DESCENDING_CONFIRMATIONS):
                        self._park_return_confirmed = True
                        self._park_return_start_position = max(
                            position,
                            PARK_RAMP_TARGET_POSITION + 0.1,
                        )

            self._sync_simulation(speed, depth, stroke, sensation,
                                  pattern_index, in_engine)
            if self._park_blocked and self._is_park_position(position):
                self._park_blocked = False
                self._pos = 0.0
                self._update_state(
                    pct=0.0, parked=True, park_blocked=False, park_error="",
                )
            if self._parking and position is not None:
                # During parking, show measured firmware position rather than
                # the normal host-side simulation.
                self._pos = max(0.0, min(100.0,
                    (position / PARK_POSITION_HIGH) * 100.0))
                self._sim_running = False
                self._move_active = False

        homed = is_homed_state(fw_state)
        self._update_state(
            fw_state=fw_state,
            in_stroke_engine=in_engine,
            homed=homed,
            # The stock firmware has no separate "engine ready" signal; once it
            # is homed and linked it will accept a mode change, which is what
            # the UI gate actually cares about.
            engineReady=bool(self._state.connected and homed
                             and not self._parking and not self._park_blocked),
            fw_speed=speed,
            fw_depth=depth,
            fw_stroke=stroke,
            fw_sensation=sensation,
            fw_pattern=pattern_index,
            position_mm=position,
            parking=self._parking,
            paused=self._motion_paused,
            parked=(bool(self._state.extra.get("parked", False))
                    and not self._parking and not self._park_blocked),
            park_mode=self._park_mode,
            park_blocked=self._park_blocked,
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
                if self._parking or self._park_blocked:
                    pass
                else:
                    self._resume_motion_locked("user_resume")
                    self._want_running = True
                    self._state.emergency_stopped = False
                    self._update_state(parked=False, paused=False, park_error="")
            elif cmd == "stopPattern":
                self._request_motion_pause_locked("user_pause")
            elif cmd == "stop":
                if self._parking:
                    self._abort_park_locked(
                        to_menu=True,
                        reason="second arrêt pendant auto-park",
                    )
                else:
                    self._request_park_locked("stop")
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
            # Narrative AI stop is only a temporary speed-zero hold.  Ignore
            # the accompanying speed=0 so the previous settings remain available
            # until the next AI turn.  It must never trigger auto-park.
            if pattern == "stop":
                self._request_motion_pause_locked("ai_stop")
                return

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

            if pattern is not None:
                index = AI_TO_DEVICE_PATTERN_MAP.get(pattern, 0)
                if index >= 0:
                    self._desired["pattern"] = index
                if self._parking or self._park_blocked:
                    pass
                else:
                    self._resume_motion_locked("ai_pattern")
                    self._want_running = True
                    self._state.emergency_stopped = False
                    self._update_state(parked=False, paused=False, park_error="")


    def emergency_stop(self) -> None:
        with self._lock:
            self._parking = False
            self._motion_paused = False
            self._command_epoch += 1
            self._park_mode = ""
            self._park_phase = ""
            self._park_saved_desired.clear()
            self._park_blocked = False
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
                           engineReady=False, parking=False, parked=False,
                           paused=False, park_mode="", park_error="arrêt d’urgence",
                           park_blocked=False)
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
