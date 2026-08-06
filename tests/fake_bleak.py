"""
tests/fake_bleak.py
-------------------
A stand-in for the bits of bleak that devices/ossm_ble.py touches.

The point is to exercise the real driver — its threads, its asyncio loop, its
writer and reconciler — against a fake transport, so the assertions are about
the *command strings and their ordering*, which is where the firmware-specific
bugs live. Nothing here models BLE semantics beyond what the driver uses.

Usage:

    client = install(monkeypatch)      # patches devices.ossm_ble.BleakClient
    device.connect("AA:BB:CC:DD:EE:FF")
    client = FakeBleakClient.latest()
    client.push_state(state="menu.idle")
    wait_for(lambda: "go:strokeEngine" in client.commands())
"""

import asyncio
import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


class FakeBleakError(Exception):
    """Stands in for BleakError so write-failure paths can be exercised."""


class FakeBleakClient:
    """Records writes and lets a test push notifications back."""

    _instances: List["FakeBleakClient"] = []
    _instances_lock = threading.Lock()

    # Set by a test to make __aenter__ raise, exercising the reconnect loop.
    connect_error: Optional[Exception] = None

    # By default a read echoes back whatever was last written, as the real
    # config characteristics do. Set this to stand in for firmware that
    # predates a characteristic and ignores the write. Class-level so a test
    # can arrange it before connect(), which is otherwise a race.
    knob_readback_override: Optional[bytes] = None

    def __init__(self, address: str, *args: Any, **kwargs: Any):
        self.address = address
        self.is_connected = False
        self.writes: List[Tuple[str, bytes]] = []
        self.notify_callbacks: Dict[str, Callable[[Any, bytearray], None]] = {}
        self.write_error: Optional[Exception] = None
        self._lock = threading.Lock()
        with FakeBleakClient._instances_lock:
            FakeBleakClient._instances.append(self)

    # ── Class-level helpers ───────────────────────────────────────────────────

    @classmethod
    def reset(cls) -> None:
        with cls._instances_lock:
            cls._instances.clear()
        cls.connect_error = None
        cls.knob_readback_override = None

    @classmethod
    def latest(cls, timeout: float = 5.0) -> "FakeBleakClient":
        """The most recently constructed client, waiting for one if needed."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with cls._instances_lock:
                if cls._instances:
                    return cls._instances[-1]
            time.sleep(0.01)
        raise AssertionError("no FakeBleakClient was constructed")

    @classmethod
    def instance_count(cls) -> int:
        with cls._instances_lock:
            return len(cls._instances)

    # ── bleak surface ─────────────────────────────────────────────────────────

    async def __aenter__(self) -> "FakeBleakClient":
        if FakeBleakClient.connect_error is not None:
            raise FakeBleakClient.connect_error
        self.is_connected = True
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        self.is_connected = False
        return False

    async def connect(self) -> bool:
        self.is_connected = True
        return True

    async def disconnect(self) -> bool:
        self.is_connected = False
        return True

    async def start_notify(self, char: Any, callback: Callable) -> None:
        self.notify_callbacks[str(char).lower()] = callback

    async def stop_notify(self, char: Any) -> None:
        self.notify_callbacks.pop(str(char).lower(), None)

    async def write_gatt_char(self, char: Any, data: Any,
                              response: bool = False) -> None:
        if self.write_error is not None:
            raise self.write_error
        with self._lock:
            self.writes.append((str(char).lower(), bytes(data)))

    async def read_gatt_char(self, char: Any) -> bytearray:
        from devices.ossm_ble import SPEED_KNOB_CHAR
        if str(char).lower() == SPEED_KNOB_CHAR.lower():
            if FakeBleakClient.knob_readback_override is not None:
                return bytearray(FakeBleakClient.knob_readback_override)
            written = self.writes_to(SPEED_KNOB_CHAR)
            return bytearray(written[-1].encode("utf-8") if written else b"")
        return bytearray()

    # ── Test-facing helpers ───────────────────────────────────────────────────

    def writes_to(self, char: str) -> List[str]:
        """Decoded payloads written to one characteristic, in order."""
        target = str(char).lower()
        with self._lock:
            return [payload.decode("utf-8", "ignore")
                    for uuid, payload in self.writes if uuid == target]

    def commands(self) -> List[str]:
        from devices.ossm_ble import COMMAND_CHAR
        return self.writes_to(COMMAND_CHAR)

    def clear_writes(self) -> None:
        with self._lock:
            self.writes.clear()

    def notify(self, char: str, payload: bytes) -> None:
        callback = self.notify_callbacks.get(str(char).lower())
        if callback is None:
            raise AssertionError(f"nothing subscribed to {char}")
        callback(None, bytearray(payload))

    def push_state(self, state: str = "menu.idle", speed: int = 0,
                   stroke: int = 50, depth: int = 10, sensation: int = 50,
                   pattern: int = 0, position: float = 0.0,
                   session_id: str = "session-1", buffer: int = 100,
                   timestamp: int = 0) -> None:
        """Send one getCurrentState()-shaped notification."""
        from devices.ossm_ble import STATE_CHAR
        payload = {
            "timestamp": timestamp,
            "state": state,
            "speed": speed,
            "stroke": stroke,
            "sensation": sensation,
            "depth": depth,
            "buffer": buffer,
            "pattern": pattern,
            "position": position,
            "sessionId": session_id,
        }
        self.notify(STATE_CHAR, json.dumps(payload).encode("utf-8"))


class FakeAdvertisementData:
    def __init__(self, local_name: str = "", service_uuids: Optional[List[str]] = None,
                 rssi: int = -60):
        self.local_name = local_name
        self.service_uuids = service_uuids or []
        self.rssi = rssi


class FakeBLEDevice:
    def __init__(self, address: str, name: str = ""):
        self.address = address
        self.name = name


class FakeBleakScanner:
    """Returns a canned discovery result in the return_adv=True shape."""

    discovered: Dict[str, Tuple[FakeBLEDevice, FakeAdvertisementData]] = {}

    @classmethod
    def set_discovered(cls, entries: List[Tuple[str, str, List[str]]]) -> None:
        cls.discovered = {
            address: (FakeBLEDevice(address, name),
                      FakeAdvertisementData(name, uuids))
            for address, name, uuids in entries
        }

    @classmethod
    async def discover(cls, timeout: float = 5.0, return_adv: bool = False,
                       **kwargs: Any):
        await asyncio.sleep(0)
        if return_adv:
            return dict(cls.discovered)
        return [device for device, _ in cls.discovered.values()]


def install(monkeypatch) -> None:
    """Point devices.ossm_ble at the fakes and reset their state."""
    import devices.ossm_ble as ossm_ble

    FakeBleakClient.reset()
    FakeBleakScanner.discovered = {}
    monkeypatch.setattr(ossm_ble, "BleakClient", FakeBleakClient)
    monkeypatch.setattr(ossm_ble, "BleakScanner", FakeBleakScanner)


def wait_for(predicate: Callable[[], bool], timeout: float = 3.0,
             interval: float = 0.01) -> bool:
    """Poll until predicate holds. Returns False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
