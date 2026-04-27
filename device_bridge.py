"""
device_bridge.py
----------------
Threaded client for the OSSM device (WebSocket or serial) with auto-reconnect.
"""

import asyncio
import json
import logging
import queue
import threading
import time
from typing import Callable, Optional

import websockets

from config import AI_TO_DEVICE_PATTERN_MAP

try:
    import serial
except ImportError:
    serial = None

log = logging.getLogger(__name__)

RECONNECT_DELAY_INITIAL = 1.0
RECONNECT_DELAY_MAX = 30.0
HEARTBEAT_INTERVAL = 5.0
HEARTBEAT_TIMEOUT = 10.0


class DeviceBridge:
    def __init__(self):
        self.ws_url: Optional[str] = None
        self.connected = False
        self.latest_state = {
            "pct": 0.0,
            "steps": 0,
            "running": False,
            "homed": False,
            "engineReady": False,
            "connected": False,
        }

        self._send_queue: queue.Queue = queue.Queue()
        self._listeners: list[Callable] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._pattern_running = False
        self._last_depth = 50
        self._last_base = 0

        self._reconnect_delay = RECONNECT_DELAY_INITIAL
        self._connection_attempts = 0

        # Serial-only queues/threads
        self._serial_write_queue: queue.Queue = queue.Queue(maxsize=50)
        self._serial_writer_thread: Optional[threading.Thread] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self, url: str) -> bool:
        if self.connected:
            self.disconnect()

        self.ws_url = url
        self._stop_event.clear()
        self._reconnect_delay = RECONNECT_DELAY_INITIAL
        self._connection_attempts = 0

        if self._looks_like_serial(url):
            return self._connect_serial(url)
        else:
            return self._connect_ws(url)

    def disconnect(self):
        self._stop_event.set()
        self.connected = False
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

    # ── Public API ────────────────────────────────────────────────────────────

    def send(self, cmd: dict):
        """Queue a command. Never blocks the caller."""
        if getattr(self, 'ser', None) and self.ser.is_open:
            try:
                self._serial_write_queue.put_nowait(cmd)
                log.debug("Device serial << %s", cmd)
            except queue.Full:
                log.warning("Serial write queue full, dropping command")
        elif self.connected:
            self._send_queue.put(cmd)

    def add_listener(self, callback: Callable):
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable):
        if callback in self._listeners:
            self._listeners.remove(callback)

    def apply_ai_commands(self, commands: dict):
        if not self.connected:
            return

        pattern = commands.get("pattern")
        speed = commands.get("speed")
        depth = commands.get("depth")
        base = commands.get("base")
        intensity = commands.get("intensity")
        manual_cmds = commands.get("commands")

        device_cmds: list[dict] = []

        if pattern == "stop":
            device_cmds.append({"cmd": "stopPattern"})
            self._pattern_running = False
        elif pattern is not None:
            idx = AI_TO_DEVICE_PATTERN_MAP.get(pattern, 0)
            device_cmds.append({"cmd": "setPattern", "value": idx})
            if not self._pattern_running:
                device_cmds.append({"cmd": "startPattern"})
                self._pattern_running = True

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
            self.send(c)
            time.sleep(0.01)

    # ── WebSocket path ────────────────────────────────────────────────────────

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
            delay = min(
                RECONNECT_DELAY_INITIAL * (2 ** (self._connection_attempts - 1)),
                RECONNECT_DELAY_MAX
            )
            log.info("Device reconnecting in %.1fs (attempt %d)", delay, self._connection_attempts)
            await asyncio.sleep(delay)

    async def _connect_and_serve(self):
        log.info("Device connecting to %s", self.ws_url)
        try:
            async with websockets.connect(
                self.ws_url,
                ping_interval=HEARTBEAT_INTERVAL,
                ping_timeout=HEARTBEAT_TIMEOUT,
                close_timeout=2,
            ) as ws:
                self.connected = True
                self._connection_attempts = 0
                with self._lock:
                    self.latest_state["connected"] = True
                log.info("Device WS connected")

                recv_t = asyncio.create_task(self._recv_loop(ws))
                send_t = asyncio.create_task(self._send_loop(ws))
                await asyncio.gather(recv_t, send_t)

        except Exception as exc:
            log.warning("Device WS disconnected: %s", exc)
        finally:
            self.connected = False
            with self._lock:
                self.latest_state["connected"] = False

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

    # ── Serial path ───────────────────────────────────────────────────────────

    def _looks_like_serial(self, url: str) -> bool:
        if not serial:
            return False
        return any(url.startswith(p) for p in ('/dev/', 'COM', 'tty', '/tmp/'))

    def _connect_serial(self, port: str) -> bool:
        try:
            # write_timeout prevents indefinite blocking on a dead device
            self.ser = serial.Serial(port, 115200, timeout=0.1, write_timeout=1.0)
            self.connected = True
            self._connection_attempts = 0
            self._reconnect_delay = RECONNECT_DELAY_INITIAL
            with self._lock:
                self.latest_state["connected"] = True
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
        """Dedicated thread: drains the write queue to the serial port."""
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
        """
        Read loop with auto-reconnect.
        Exits only when disconnect() is called.
        """
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

            # Mark disconnected
            self.connected = False
            with self._lock:
                self.latest_state["connected"] = False

            if self._stop_event.is_set():
                break

            # Backoff reconnect
            self._connection_attempts += 1
            delay = min(
                RECONNECT_DELAY_INITIAL * (2 ** (self._connection_attempts - 1)),
                RECONNECT_DELAY_MAX
            )
            log.info("Serial reconnecting in %.1fs (attempt %d)", delay, self._connection_attempts)
            time.sleep(delay)

            try:
                if getattr(self, 'ser', None):
                    try:
                        self.ser.close()
                    except Exception:
                        pass
                self.ser = serial.Serial(self.ws_url, 115200, timeout=0.1, write_timeout=1.0)
                self.connected = True
                with self._lock:
                    self.latest_state["connected"] = True
                log.info("Serial reconnected on %s", self.ws_url)
            except Exception as exc:
                log.error("Serial reconnect failed: %s", exc)

        log.info("Device serial thread exiting")

    # ── Shared ────────────────────────────────────────────────────────────────

    def _handle_message(self, data: dict):
        if data.get("type") == "position":
            with self._lock:
                self.latest_state.update({
                    "pct": data.get("pct", 0),
                    "steps": data.get("steps", 0),
                    "running": data.get("running", False),
                    "homed": data.get("homed", False),
                    "engineReady": data.get("engineReady", False),
                })
            for cb in self._listeners:
                try:
                    cb(data)
                except Exception:
                    pass


_device_bridge = DeviceBridge()


def get_bridge() -> DeviceBridge:
    return _device_bridge