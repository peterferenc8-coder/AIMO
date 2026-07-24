"""
device_bridge.py
----------------
Backward-compatible wrapper around the new device package.
All logic has moved to devices/ossm.py.
This module proxies to the registry singleton so there is only ONE OSSM instance.
"""

from devices.registry import get_active_device


def get_bridge():
    """Legacy singleton accessor. Returns the currently active device."""
    dev = get_active_device()
    if dev is None:
        # Fallback: lazy-init the no-op device if the registry hasn't been set
        # up yet, so nothing moves before the user has chosen a toy.
        from devices.registry import set_active_device
        dev = set_active_device("none")
    return dev
