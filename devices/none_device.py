"""
devices/none_device.py
----------------------
The "No Toy" driver: a device that isn't there.

Selected by default so the app is usable — chat, avatar, patterns, funscript
playback — before any hardware is paired. It reports itself connected and
homed from the start, which is what unlocks the rest of the tabs, and then
swallows every command it is given.
"""

import logging
from typing import Any, Dict, Optional

from .base import AbstractDevice

log = logging.getLogger(__name__)


class NoneDevice(AbstractDevice):
    name = "No Toy"
    device_type = "none"

    def __init__(self):
        super().__init__()
        # Present as a fully set-up device: nothing here has to be connected,
        # zeroed or homed, and the UI keys its gating off exactly these flags.
        self._update_state(
            connected=True,
            pct=0.0,
            steps=0,
            running=False,
            homed=True,
            engineReady=True,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self, address: Optional[str] = None) -> bool:
        self._update_state(connected=True)
        return True

    def disconnect(self) -> None:
        # Stays "connected": there is nothing to drop, and going offline would
        # only re-lock the tabs this device exists to keep open.
        pass

    # ── Commands ─────────────────────────────────────────────────────────────

    def send_command(self, command: Dict[str, Any]) -> None:
        """Swallow the command, but mirror position moves onto the gauge.

        A funscript or a pattern preview is worth watching on screen even with
        no hardware attached, and moveTo/stream are the commands that carry a
        position the UI can show.
        """
        cmd = command.get("cmd")
        if cmd in ("moveTo", "stream"):
            try:
                pct = float(command.get("pct", 0.0))
            except (TypeError, ValueError):
                return
            self._update_state(pct=max(0.0, min(100.0, pct)), running=True)
        elif cmd in ("stop", "stopPattern"):
            self._update_state(running=False)
        else:
            log.debug("No Toy ignores command: %s", command)

    def emergency_stop(self) -> None:
        self._update_state(running=False, emergency_stopped=True)
