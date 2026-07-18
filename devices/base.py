"""
devices/base.py
---------------
Abstract base class for all physical devices.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class DeviceState:
    connected: bool = False
    emergency_stopped: bool = False
    device_type: str = "unknown"
    # Subclasses add fields via extra dict
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "connected": self.connected,
            "emergency_stopped": self.emergency_stopped,
            "device_type": self.device_type,
            **self.extra,
        }


class AbstractDevice(ABC):
    name: str = "abstract"
    device_type: str = "unknown"

    def __init__(self):
        self._state = DeviceState(device_type=self.device_type)
        self._listeners: List[Callable[[dict], None]] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @abstractmethod
    def connect(self, address: Optional[str] = None) -> bool:
        """Connect to the device. Returns True on success."""
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect and clean up."""
        ...

    # ── Commands ─────────────────────────────────────────────────────────────

    @abstractmethod
    def send_command(self, command: Dict[str, Any]) -> None:
        """Send a command dict to the device. Never blocks caller."""
        ...

    @abstractmethod
    def emergency_stop(self) -> None:
        """Immediate emergency stop. Must be synchronous and fast."""
        ...

    def apply_ai_commands(self, commands: Dict[str, Any]) -> None:
        """Apply a compiled intent from the orchestrator.

        `commands` carries {pattern, speed, depth, base, intensity, commands},
        as produced by intent_compiler. Concrete (not abstract) and a no-op by
        default: the orchestrator calls this on whatever device is active, so a
        driver that has no meaningful mapping should simply sit out an AI
        session rather than raise.
        """
        log.debug("%s ignores AI commands: %s", self.device_type, commands)

    # ── State ──────────────────────────────────────────────────────────────────

    def get_state(self) -> DeviceState:
        return self._state

    @property
    def latest_state(self) -> dict:
        """Backward-compatible dict accessor."""
        return self._state.as_dict()

    def _update_state(self, **kwargs) -> None:
        for k, v in kwargs.items():
            if hasattr(self._state, k):
                setattr(self._state, k, v)
            else:
                self._state.extra[k] = v
        self._notify_listeners()

    def add_listener(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[dict], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify_listeners(self) -> None:
        payload = self._state.as_dict()
        for cb in list(self._listeners):
            try:
                cb(payload)
            except Exception:
                pass
