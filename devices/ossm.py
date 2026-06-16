"""
devices/ossm.py
---------------
OSSM linear actuator device (WebSocket or Serial).
Refactored from the original device_bridge.py.
"""

import asyncio
import json
import logging
import queue
import threading
import time
from typing import Any, Dict, Optional

import websockets

from config import AI_TO_DEVICE_PATTERN_MAP
from .base import AbstractDevice

try:
    import serial
except ImportError:
    serial = None

log = logging.getLogger(__name__)

RECONNECT_DELAY_INITIAL = 1.0
RECONNECT_DELAY_MAX = 30.0
HEARTBEAT_INTERVAL = 5.0
HEARTBEAT_TIMEOUT = 10.0


class OSSMDevice(AbstractDevice):
    name = "OSSM Linear Actuator"
    device_type = "ossm"

    def __init__(self):
        super().__init__()
        self.ws_url: Optional[str] = None
        self._send_queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._pattern_running = False
        self._last_depth = 50
        self._last_base = 0
        self._reconnect_delay = RECONNECT_DELAY_INITIAL
        self._connection_attempts = 0

        # Serial-only
        self._serial_write_queue: queue.Queue = queue.Queue(maxsize=50)
        self._serial_writer_thread: Optional[threading.Thread] = None

        self._update_state(
            pct=0.0, steps=0, running=False, homed=False,
            engineReady=False,
        )

    # ── Backward-compatible aliases ───────────────────────────────────────────

    def send(self, cmd: dict) -> None:
        self.send_command(cmd)

    # ── AbstractDevice API ────────────────────────────────────────────────────

    def connect(self, address: Optional[str] = None) -> bool:
        if self._state.connected:
            self.disconnect()
        self.ws_url = address
        self._stop_event.clear()
        self._reconnect_delay = RECONNECT_DELAY_INITIAL
        self._connection_attempts = 0

        if address and self._looks_like_serial(address):
            return self._connect_serial(address)
        else:
            return self._connect_ws(address)

    def disconnect(self):
        self._stop_event.set()
        self._update_state(connected=False)
        if getattr(self, 'ser', None):
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        if self._thread:
            self._thread.join(timeout=3)
        if self._serial_writer_thread:
            self._serial_writer_thread.join(timeout=1)

    def send_command(self, command: Dict[str, Any]) -> None:
        """Send a raw command dict."""
        if getattr(self, 'ser', None) and self.ser.is_open:
            try:
                self._serial_write_queue.put_nowait(command)
            except queue.Full:
                log.warning("Serial write queue full")
        elif self._state.connected:
            self._send_queue.put(command)

    def emergency_stop(self) -> None:
        self.send_command({"cmd": "stopPattern"})
        self.send_command({"cmd": "stop"})
        self._pattern_running = False
        self._update_state(emergency_stopped=True, running=False)
        log.warning("OSSM EMERGENCY STOP")

    # ── Legacy orchestrator helper ────────────────────────────────────────────

    def apply_ai_commands(self, commands: dict):
        """Legacy method used by orchestrator."""
        if not self._state.connected:
            return

        pattern = commands.get("pattern")
        speed = commands.get("speed")
        depth = commands.get("depth")
        base = commands.get("base")
        intensity = commands.get("intensity")
        manual_cmds = commands.get("commands")

        device_cmds: list[dict] = []

        # Apply motion parameters BEFORE selecting/starting the pattern.
        # startPattern() on the device pushes the current depth/stroke/speed
        # globals into the engine, so these must already be set — otherwise the
        # pattern starts at the stale default depth (100%) and the carriage
        # lunges fully "in" before the real (lower) depth takes effect, which
        # reads as the gauge starting at 100% and slowly drifting to 0.
        if speed is not None:
            device_cmds.append({"cmd": "setSpeedPct", "value": speed})

        if depth is not None:
            device_cmds.append({"cmd": "setDepthPct", "value": depth})
            self._last_depth = depth

        if base is not None:
            self._last_base = base

        if base is not None or depth is not None:
            stroke = max(0, self._last_depth - self._last_base)
            device_cmds.append({"cmd": "setStrokePct", "value": stroke})

        if intensity is not None:
            device_cmds.append({"cmd": "setSensation", "value": intensity})

        # Now select/start (or stop) the pattern, with parameters already in place.
        if pattern == "stop":
            device_cmds.append({"cmd": "stopPattern"})
            self._pattern_running = False
        elif pattern is not None:
            idx = AI_TO_DEVICE_PATTERN_MAP.get(pattern, 0)
            device_cmds.append({"cmd": "setPattern", "value": idx})
            if not self._pattern_running:
                device_cmds.append({"cmd": "startPattern"})
                self._pattern_running = True

        if manual_cmds:
            for cmd in manual_cmds:
                if cmd.get("name") == "moveTo":
                    args = cmd.get("args", {})
                    device_cmds.append({
                        "cmd": "moveTo",
                        "pct": args.get("pos", 50),
                        "speedPct": args.get("speed", 50),
                        "accelPct": args.get("accel", 70),
                    })

        for c in device_cmds:
            self.send_command(c)
            time.sleep(0.01)

    # ── WebSocket ─────────────────────────────────────────────────────────────

    def _connect_ws(self, url: str) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        time.sleep(0.3)
        return self._state.connected

    def _run(self):
        asyncio.run(self._async_run())

    async def _async_run(self):
        while not self._stop_event.is_set():
            try:
                await self._connect_and_serve()
            except Exception as exc:
                log.error("Device connection fatal: %s", exc)
            if self._stop_event.is_set():
                break
            self._connection_attempts += 1
            delay = min(RECONNECT_DELAY_INITIAL * (2 ** (self._connection_attempts - 1)), RECONNECT_DELAY_MAX)
            log.info("Device reconnecting in %.1fs (attempt %d)", delay, self._connection_attempts)
            await asyncio.sleep(delay)

    async def _connect_and_serve(self):
        log.info("Device connecting to %s", self.ws_url)
        try:
            async with websockets.connect(
                self.ws_url or "ws://localhost:8888",
                ping_interval=HEARTBEAT_INTERVAL,
                ping_timeout=HEARTBEAT_TIMEOUT,
                close_timeout=2,
            ) as ws:
                self._update_state(connected=True)
                self._connection_attempts = 0
                log.info("Device WS connected")
                recv_t = asyncio.create_task(self._recv_loop(ws))
                send_t = asyncio.create_task(self._send_loop(ws))
                await asyncio.gather(recv_t, send_t)
        except Exception as exc:
            log.warning("Device WS disconnected: %s", exc)
        finally:
            self._update_state(connected=False)

    async def _recv_loop(self, ws):
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self._handle_message(msg)
        except Exception as exc:
            log.warning("Device recv ended: %s", exc)

    async def _send_loop(self, ws):
        try:
            while not self._stop_event.is_set():
                try:
                    cmd = self._send_queue.get(timeout=0.05)
                    await ws.send(json.dumps(cmd))
                    log.debug("Device << %s", cmd)
                except queue.Empty:
                    continue
        except Exception as exc:
            log.warning("Device send ended: %s", exc)

    # ── Serial ────────────────────────────────────────────────────────────────

    def _looks_like_serial(self, url: str) -> bool:
        if not serial:
            return False
        return any(url.startswith(p) for p in ('/dev/', 'COM', 'tty', '/tmp/'))

    def _connect_serial(self, port: str) -> bool:
        try:
            self.ser = serial.Serial(port, 115200, timeout=0.1, write_timeout=1.0)
            self._update_state(connected=True)
            self._connection_attempts = 0
            log.info("Device serial connected on %s", port)
            self._thread = threading.Thread(target=self._serial_run, daemon=True)
            self._thread.start()
            self._serial_writer_thread = threading.Thread(target=self._serial_writer_run, daemon=True)
            self._serial_writer_thread.start()
            return True
        except Exception as exc:
            log.error("Serial connection failed: %s", exc)
            return False

    def _serial_writer_run(self):
        while not self._stop_event.is_set() and getattr(self, 'ser', None) and self.ser.is_open:
            try:
                cmd = self._serial_write_queue.get(timeout=0.2)
                data = (json.dumps(cmd) + '\n').encode('utf-8')
                self.ser.write(data)
            except queue.Empty:
                continue
            except Exception as exc:
                log.warning("Serial write error: %s", exc)
                time.sleep(0.1)

    def _serial_run(self):
        while not self._stop_event.is_set():
            buffer = ""
            while not self._stop_event.is_set() and getattr(self, 'ser', None) and self.ser.is_open:
                try:
                    if self.ser.in_waiting:
                        data = self.ser.read(self.ser.in_waiting)
                        buffer += data.decode('utf-8', errors='ignore')
                        while '\n' in buffer:
                            line, buffer = buffer.split('\n', 1)
                            line = line.strip()
                            if line.startswith('{'):
                                try:
                                    msg = json.loads(line)
                                    self._handle_message(msg)
                                except json.JSONDecodeError:
                                    pass
                    else:
                        time.sleep(0.01)
                except (OSError, serial.SerialException) as exc:
                    log.error("Serial port disconnected: %s", exc)
                    break
                except Exception as exc:
                    log.warning("Serial read error: %s", exc)
                    time.sleep(0.1)

            self._update_state(connected=False)
            if self._stop_event.is_set():
                break

            self._connection_attempts += 1
            delay = min(RECONNECT_DELAY_INITIAL * (2 ** (self._connection_attempts - 1)), RECONNECT_DELAY_MAX)
            log.info("Serial reconnecting in %.1fs (attempt %d)", delay, self._connection_attempts)
            time.sleep(delay)

            try:
                if getattr(self, 'ser', None):
                    try:
                        self.ser.close()
                    except Exception:
                        pass
                self.ser = serial.Serial(self.ws_url, 115200, timeout=0.1, write_timeout=1.0)
                self._update_state(connected=True)
                log.info("Serial reconnected on %s", self.ws_url)
            except Exception as exc:
                log.error("Serial reconnect failed: %s", exc)

        log.info("Device serial thread exiting")

    # ── Override base notification to avoid double-fire ────────────────────────

    def _update_state(self, **kwargs) -> None:
        """Update state without notifying listeners — OSSM handles that via raw message forwarding."""
        for k, v in kwargs.items():
            if hasattr(self._state, k):
                setattr(self._state, k, v)
            else:
                self._state.extra[k] = v
        # Don't call _notify_listeners here; _handle_message does raw forwarding

    # ── Shared ─────────────────────────────────────────────────────────────────

    def _handle_message(self, data: dict):
        if data.get("type") == "position":
            self._update_state(
                pct=data.get("pct", 0),
                steps=data.get("steps", 0),
                running=data.get("running", False),
                homed=data.get("homed", False),
                engineReady=data.get("engineReady", False),
            )
            # Preserve the original message type for listeners
            data = dict(data)
            data.setdefault("type", "position")
        # Forward the raw message to listeners (for SSE stream)
        for cb in list(self._listeners):
            try:
                cb(data)
            except Exception:
                pass
