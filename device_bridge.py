"""
device_bridge.py
----------------
Backward-compatible wrapper around the new device package.
All logic has moved to devices/ossm.py.
This module proxies to the registry singleton so there is only ONE OSSM instance.
"""

from devices.registry import get_active_device


def get_bridge():
    """Legacy singleton accessor. Returns the currently active device (usually OSSM)."""
    dev = get_active_device()
    if dev is None:
        # Fallback: lazy-init OSSM if registry hasn't been set up yet
        from devices.registry import set_active_device
        dev = set_active_device("ossm")
    return dev
