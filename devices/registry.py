"""
devices/registry.py
-------------------
Device factory and active device singleton.
"""

import logging
from typing import Dict, Optional, Type

from .base import AbstractDevice
from .ossm import OSSMDevice
from .coyote_ble import CoyoteBLE

log = logging.getLogger(__name__)

_REGISTRY: Dict[str, Type[AbstractDevice]] = {
    "ossm": OSSMDevice,
    "coyote": CoyoteBLE,
}

_active_device: Optional[AbstractDevice] = None
_active_type: str = "ossm"


def list_device_types() -> list[dict]:
    return [
        {"id": "ossm", "name": "OSSM (Linear Actuator)"},
        {"id": "coyote", "name": "DG-Lab Coyote 3.0 (E-Stim BLE)"},
    ]


def set_active_device(device_type: str, **kwargs) -> AbstractDevice:
    global _active_device, _active_type
    if _active_device:
        try:
            _active_device.disconnect()
        except Exception as exc:
            log.warning("Error disconnecting previous device: %s", exc)

    cls = _REGISTRY.get(device_type)
    if not cls:
        raise ValueError(f"Unknown device type: {device_type}")

    _active_type = device_type
    _active_device = cls(**kwargs)
    log.info("Active device set to %s", device_type)
    return _active_device


def get_active_device() -> Optional[AbstractDevice]:
    return _active_device


def get_active_type() -> str:
    return _active_type
