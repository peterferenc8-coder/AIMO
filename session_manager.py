"""
session_manager.py
------------------
Manages the in-memory state of a generation session:
  - The list of generated turns
  - The current "effective" device state (last non-null values of each param)
  - Convenience accessors used by the Flask routes and the GUI template

There is intentionally no database here – state lives in memory for the
lifetime of the Flask process. For persistence, serialise/deserialise via
the to_dict / from_dict methods.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from response_parser import Turn, Commands

log = logging.getLogger(__name__)


@dataclass
class DeviceState:
    """
    Tracks the last *effective* value of each device parameter.

    When a command uses null for a parameter, the device keeps the previous
    value — this class models that continuity.
    """
    pattern:   str | None = None
    speed:     int | None = None
    intensity: int | None = None
    depth:     int | None = None
    base:      int | None = None

    def apply(self, commands: Commands) -> None:
        """Merge a Commands object into the running state (null = no change)."""
        if commands.pattern   is not None:
            self.pattern = commands.pattern
        if commands.speed     is not None:
            self.speed = commands.speed
        if commands.intensity is not None:
            self.intensity = commands.intensity
        if commands.depth     is not None:
            self.depth = commands.depth
        if commands.base      is not None:
            self.base = commands.base

    def as_dict(self) -> dict:
        return {
            "pattern":   self.pattern,
            "speed":     self.speed,
            "intensity": self.intensity,
            "depth":     self.depth,
            "base":      self.base,
        }


class SessionManager:
    """
    Holds all turns for the current session and the running device state.

    The Flask app keeps one SessionManager alive per process.
    """

    def __init__(self):
        self._turns: list[Turn] = []
        self._device = DeviceState()

    # ── Mutation ──────────────────────────────────────────────────────────────

    def add_turns(self, turns: list[Turn]) -> None:
        """Append new turns and update the effective device state."""
        for turn in turns:
            self._device.apply(turn.commands)
            self._turns.append(turn)
        log.debug("Added %d turns; total=%d", len(turns), len(self._turns))

    def clear(self) -> None:
        """Reset session to empty."""
        self._turns = []
        self._device = DeviceState()
        log.info("Session cleared")

    # ── Read ──────────────────────────────────────────────────────────────────

    @property
    def turns(self) -> list[Turn]:
        """All turns in chronological order."""
        return list(self._turns)

    @property
    def device_state(self) -> DeviceState:
        """The current effective device state."""
        return self._device

    def turns_as_dicts(self) -> list[dict[str, Any]]:
        """Serialise all turns for JSON responses / template rendering."""
        return [turn.as_dict() for turn in self._turns]

    def to_dict(self) -> dict:
        """Full session snapshot."""
        return {
            "turns":        self.turns_as_dicts(),
            "device_state": self._device.as_dict(),
            "total_turns":  len(self._turns),
        }