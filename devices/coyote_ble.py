"""
devices/coyote_ble.py
---------------------
DG-Lab Coyote 3.0 direct BLE control via bleak.
Implements the V3 Bluetooth protocol.
"""

import asyncio
import logging
import threading
import time
from typing import Any, Dict, List, Optional

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    BleakClient = None  # type: ignore
    BleakScanner = None  # type: ignore

from .base import AbstractDevice

log = logging.getLogger(__name__)

# DG-Lab Coyote 3.0 BLE UUIDs (base UUID pattern)
SERVICE_UUID = "0000180c-0000-1000-8000-00805f9b34fb"
WRITE_CHAR = "0000150a-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000150b-0000-1000-8000-00805f9b34fb"
BATTERY_SERVICE = "0000180a-0000-1000-8000-00805f9b34fb"
BATTERY_CHAR = "00001500-0000-1000-8000-00805f9b34fb"
DEVICE_NAME = "47L121000"


class CoyoteBLE(AbstractDevice):
    name = "DG-Lab Coyote 3.0"
    device_type = "coyote"

    def __init__(self):
        super().__init__()
        self._client: Optional[Any] = None
        self._address: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Desired output state (protected by _lock)
        self._lock = threading.Lock()
        self._ch_a_strength = 0
        self._ch_b_strength = 0
        self._ch_a_freq_ms = 100      # 10-1000
        self._ch_b_freq_ms = 100
        self._ch_a_wave_strength = 50  # 0-100
        self._ch_b_wave_strength = 50
        self._soft_limit_a = 100
        self._soft_limit_b = 100
        self._freq_balance_a = 150
        self._freq_balance_b = 150
        self._str_balance_a = 150
        self._str_balance_b = 150

        # Waveform preset cycling
        self._waveform_preset = "steady"
        self._waveform_tick = 0

        # Battery
        self._battery = 0

        if BleakClient is None:
            log.error("bleak is not installed. Coyote BLE will not work.")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self, address: Optional[str] = None) -> bool:
        if not BleakClient:
            log.error("bleak not installed")
            return False

        if self._client and getattr(self._client, "is_connected", False):
            self.disconnect()

        self._address = address
        self._stop_event.clear()

        self._thread = threading.Thread(target=self._ble_thread, daemon=True)
        self._thread.start()

        # Wait up to 5 seconds for connection attempt
        for _ in range(50):
            if self._state.connected:
                return True
            if self._stop_event.is_set():
                return False
            time.sleep(0.1)
        return self._state.connected

    def disconnect(self):
        self._stop_event.set()
        if self._loop:
            try:
                future = asyncio.run_coroutine_threadsafe(self._disconnect_async(), self._loop)
                future.result(timeout=5)
            except Exception:
                pass

        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

        self._update_state(connected=False)
        log.info("Coyote disconnected")

    async def _disconnect_async(self):
        if self._client and getattr(self._client, "is_connected", False):
            try:
                await self._client.disconnect()
            except Exception as exc:
                log.warning("Coyote disconnect error: %s", exc)
        self._client = None

    def _ble_thread(self):
        """Runs the asyncio event loop for BLE operations."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ble_main())
        except Exception as exc:
            log.error("Coyote BLE thread error: %s", exc)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None

    async def _ble_main(self):
        while not self._stop_event.is_set():
            try:
                if not self._address:
                    log.warning("No Coyote BLE address provided")
                    await asyncio.sleep(1)
                    continue

                log.info("Coyote connecting to %s", self._address)
                async with BleakClient(self._address) as client:
                    self._client = client
                    self._update_state(connected=True, engineReady=True, homed=True)
                    log.info("Coyote BLE connected")

                    # Subscribe to notifications
                    await client.start_notify(NOTIFY_CHAR, self._on_notify)

                    # Write soft limits (BF command) - required after reconnect
                    await self._send_bf()

                    # Start output loop
                    output_task = asyncio.create_task(self._output_loop())
                    await self._keep_alive()
                    output_task.cancel()
                    try:
                        await output_task
                    except asyncio.CancelledError:
                        pass

            except Exception as exc:
                log.warning("Coyote BLE connection lost: %s", exc)
            finally:
                self._client = None
                self._update_state(connected=False)

            if self._stop_event.is_set():
                break
            await asyncio.sleep(2)

    async def _keep_alive(self):
        while not self._stop_event.is_set():
            if not self._client or not getattr(self._client, "is_connected", False):
                break
            await asyncio.sleep(0.5)

    # ── Output Loop ───────────────────────────────────────────────────────────

    async def _output_loop(self):
        """Write B0 command every 100ms."""
        while not self._stop_event.is_set():
            try:
                if self._client and getattr(self._client, "is_connected", False):
                    packet = self._build_b0()
                    await self._client.write_gatt_char(WRITE_CHAR, packet, response=False)
            except Exception as exc:
                log.debug("Coyote write error: %s", exc)
                break
            await asyncio.sleep(0.1)

    # ── Packet Builders ───────────────────────────────────────────────────────

    def _build_b0(self) -> bytes:
        with self._lock:
            self._waveform_tick += 1

            # Sequence number 0 for manual mode (no B1 wait required)
            seq = 0
            parse_method = 0x0C  # 0b1100 = A absolute, B absolute

            a_set = max(0, min(200, self._ch_a_strength))
            b_set = max(0, min(200, self._ch_b_strength))
            a_set = min(a_set, self._soft_limit_a)
            b_set = min(b_set, self._soft_limit_b)

            a_freqs, a_strs, b_freqs, b_strs = self._get_waveform_frame()

            packet = bytearray(20)
            packet[0] = 0xB0
            packet[1] = (seq << 4) | parse_method
            packet[2] = a_set
            packet[3] = b_set
            for i in range(4):
                packet[4 + i] = a_freqs[i]
                packet[8 + i] = a_strs[i]
                packet[12 + i] = b_freqs[i]
                packet[16 + i] = b_strs[i]
            return bytes(packet)

    def _get_waveform_frame(self) -> tuple:
        """Return (a_freqs, a_strs, b_freqs, b_strs) for current 100ms slot."""
        base_a_freq = self._freq_to_output(self._ch_a_freq_ms)
        base_b_freq = self._freq_to_output(self._ch_b_freq_ms)
        base_a_str = max(0, min(100, self._ch_a_wave_strength))
        base_b_str = max(0, min(100, self._ch_b_wave_strength))

        preset = self._waveform_preset
        tick = self._waveform_tick

        if preset == "steady":
            return (
                [base_a_freq] * 4, [base_a_str] * 4,
                [base_b_freq] * 4, [base_b_str] * 4,
            )

        elif preset == "breathe":
            # Gentle ramp up/down over ~3 seconds (30 frames)
            phase = tick % 30
            if phase < 15:
                factor = phase / 14.0
            else:
                factor = (29 - phase) / 14.0
            return (
                [base_a_freq] * 4, [int(base_a_str * factor)] * 4,
                [base_b_freq] * 4, [int(base_b_str * factor)] * 4,
            )

        elif preset == "pulse":
            on = (tick % 10) < 5
            s = base_a_str if on else 0
            return (
                [base_a_freq] * 4, [s] * 4,
                [base_b_freq] * 4, [s] * 4,
            )

        elif preset == "waves":
            a_on = (tick % 20) < 10
            return (
                [base_a_freq] * 4, [base_a_str if a_on else 0] * 4,
                [base_b_freq] * 4, [base_b_str if not a_on else 0] * 4,
            )

        else:
            return (
                [base_a_freq] * 4, [base_a_str] * 4,
                [base_b_freq] * 4, [base_b_str] * 4,
            )

    def _build_bf(self) -> bytes:
        with self._lock:
            return bytes([
                0xBF,
                max(0, min(200, self._soft_limit_a)),
                max(0, min(200, self._soft_limit_b)),
                max(0, min(255, self._freq_balance_a)),
                max(0, min(255, self._freq_balance_b)),
                max(0, min(255, self._str_balance_a)),
                max(0, min(255, self._str_balance_b)),
            ])

    @staticmethod
    def _freq_to_output(val: int) -> int:
        """Convert 10-1000ms waveform frequency to 10-240 output value."""
        val = max(10, min(1000, val))
        if val <= 100:
            return val
        elif val <= 600:
            return (val - 100) // 5 + 100
        else:
            return (val - 600) // 10 + 200

    # ── Notifications ─────────────────────────────────────────────────────────

    def _on_notify(self, sender, data: bytearray):
        if not data:
            return
        try:
            if data[0] == 0xB1 and len(data) >= 4:
                seq = data[1]
                a_actual = data[2]
                b_actual = data[3]
                self._update_state(
                    ch_a_actual=a_actual,
                    ch_b_actual=b_actual,
                    battery=self._battery,
                )
                log.debug("Coyote B1 response seq=%s A=%s B=%s", seq, a_actual, b_actual)
            elif data[0] == 0xB1:
                log.debug("Short B1 message: %s", data.hex())
        except Exception as exc:
            log.warning("Coyote notify parse error: %s", exc)

    # ── Public API ────────────────────────────────────────────────────────────

    def send_command(self, command: Dict[str, Any]) -> None:
        """Accepts coyote-specific commands."""
        resend_bf = False
        with self._lock:
            if "ch_a" in command:
                self._ch_a_strength = int(command["ch_a"])
            if "ch_b" in command:
                self._ch_b_strength = int(command["ch_b"])
            if "freq_a" in command:
                self._ch_a_freq_ms = int(command["freq_a"])
            if "freq_b" in command:
                self._ch_b_freq_ms = int(command["freq_b"])
            if "wave_a" in command:
                self._ch_a_wave_strength = int(command["wave_a"])
            if "wave_b" in command:
                self._ch_b_wave_strength = int(command["wave_b"])
            if "soft_limit_a" in command:
                self._soft_limit_a = int(command["soft_limit_a"])
                resend_bf = True
            if "soft_limit_b" in command:
                self._soft_limit_b = int(command["soft_limit_b"])
                resend_bf = True
            if "waveform" in command:
                self._waveform_preset = str(command["waveform"])
            if "freq_balance_a" in command:
                self._freq_balance_a = int(command["freq_balance_a"])
                resend_bf = True
            if "freq_balance_b" in command:
                self._freq_balance_b = int(command["freq_balance_b"])
                resend_bf = True
            if "str_balance_a" in command:
                self._str_balance_a = int(command["str_balance_a"])
                resend_bf = True
            if "str_balance_b" in command:
                self._str_balance_b = int(command["str_balance_b"])
                resend_bf = True

        if resend_bf and self._loop and self._client and getattr(self._client, "is_connected", False):
            try:
                asyncio.run_coroutine_threadsafe(self._send_bf(), self._loop)
            except Exception:
                pass

    async def _send_bf(self):
        if self._client and getattr(self._client, "is_connected", False):
            try:
                packet = self._build_bf()
                await self._client.write_gatt_char(WRITE_CHAR, packet, response=False)
                log.debug("Sent BF soft limits")
            except Exception as exc:
                log.warning("BF write error: %s", exc)

    def emergency_stop(self) -> None:
        with self._lock:
            self._ch_a_strength = 0
            self._ch_b_strength = 0
            self._state.emergency_stopped = True
        # Try immediate zero write
        if self._loop and self._client and getattr(self._client, "is_connected", False):
            packet = self._build_b0()
            try:
                asyncio.run_coroutine_threadsafe(
                    self._client.write_gatt_char(WRITE_CHAR, packet, response=False),
                    self._loop,
                )
            except Exception:
                pass
        self._update_state(emergency_stopped=True, engineReady=False)
        log.warning("COYOTE EMERGENCY STOP")

    # ── Scan ───────────────────────────────────────────────────────────────────

    @staticmethod
    async def _scan_async(timeout: float = 5.0) -> List[Dict[str, Any]]:
        if not BleakScanner:
            return []
        devices = await BleakScanner.discover(timeout=timeout)
        results = []
        for d in devices:
            name = d.name or ""
            if DEVICE_NAME in name or "Coyote" in name or "DG-LAB" in name:
                results.append({
                    "address": d.address,
                    "name": name,
                    "rssi": getattr(d, "rssi", 0),
                })
        return results

    @staticmethod
    def scan(timeout: float = 5.0) -> List[Dict[str, Any]]:
        """Synchronous wrapper for scan."""
        if not BleakScanner:
            return []
        try:
            return asyncio.run(CoyoteBLE._scan_async(timeout))
        except Exception as exc:
            log.error("Coyote scan error: %s", exc)
            return []
