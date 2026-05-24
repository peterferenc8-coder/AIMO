"""
device_bridge.py
----------------
Backward-compatible wrapper around the new device package.
All logic has moved to devices/ossm.py.
"""

from devices.ossm import OSSMDevice

_ossm_device = OSSMDevice()


def get_bridge():
    """Legacy singleton accessor."""
    return _ossm_device
