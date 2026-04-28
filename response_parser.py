"""
response_parser.py
------------------
Parses the raw text coming back from the model and extracts structured
fields: speech, pattern, speed, intensity, depth, base.

The model is instructed to return a JSON list of objects. In practice
Gemma sometimes wraps the JSON in markdown fences or emits a single
object instead of a list – this module handles all those edge-cases.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Commands:
    """
    Parsed device command block.

    None means 'no change' (null in JSON). The device keeps the
    previous value when a field is null.
    """
    pattern:   str | None = None
    speed:     int | None = None
    intensity: int | None = None
    depth:     int | None = None
    base:      int | None = None

    def any_changed(self) -> bool:
        """True if at least one parameter is explicitly set."""
        return any(v is not None for v in (
            self.pattern, self.speed, self.intensity, self.depth, self.base
        ))

    def as_dict(self) -> dict:
        return {
            "pattern":   self.pattern,
            "speed":     self.speed,
            "intensity": self.intensity,
            "depth":     self.depth,
            "base":      self.base,
        }


@dataclass
class Turn:
    """One complete model turn: what was said + what the device should do."""
    index:    int
    speech:   str
    commands: Commands = field(default_factory=Commands)
    raw:      dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "index":    self.index,
            "speech":   self.speech,
            "commands": self.commands.as_dict(),
            "raw":      self.raw,
        }


# ── Parser ────────────────────────────────────────────────────────────────────

class ResponseParser:
    """
    Converts a raw model response string into a list of Turn objects.
    """

    def parse(self, raw_text: str) -> list[Turn]:
        """
        Parse raw model output into a list of Turn objects.

        Returns an empty list on total failure (logged as error).
        """
        cleaned = self._strip_markdown_fences(raw_text)
        payload = self._extract_json(cleaned)

        if payload is None:
            log.error("Could not extract JSON from model response")
            return []

        # Normalise single object to a one-element list.
        if isinstance(payload, dict):
            payload = [payload]

        if not isinstance(payload, list):
            log.error("Unexpected JSON top-level type: %s", type(payload))
            return []

        turns: list[Turn] = []
        for i, item in enumerate(payload):
            turn = self._parse_turn(i, item)
            if turn is not None:
                turns.append(turn)

        log.info("Parsed %d turn(s)", len(turns))
        return turns

    # ── JSON extraction strategies ────────────────────────────────────────────

    @staticmethod
    def _strip_markdown_fences(text: str) -> str:
        """Remove ```json … ``` or ``` … ``` wrappers."""
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return text.strip()

    def _extract_json(self, text: str) -> Any:
        # Strategy 1: direct parse (handles JSON array or single object)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Strategy 2: NDJSON — multiple {"action":...}\n{"action":...} lines
        ndjson = self._extract_ndjson(text)
        if ndjson is not None:
            return ndjson

        # Strategy 3: find first balanced list
        list_payload = self._extract_balanced(text, "[", "]")
        if list_payload is not None:
            return list_payload

        # Strategy 4: find first balanced object
        obj_payload = self._extract_balanced(text, "{", "}")
        if obj_payload is not None:
            return obj_payload

        return None

    def _extract_ndjson(self, text: str) -> list[dict] | None:
        """
        Parse newline-delimited JSON objects: {..}\n{..}\n{..}
        Returns a list of dicts, or None if no valid objects found.
        """
        objects: list[dict] = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    objects.append(obj)
            except json.JSONDecodeError:
                continue
        
        return objects if objects else None

    @staticmethod
    def _extract_balanced(text: str, open_char: str, close_char: str) -> Any:
        """
        Find the first balanced pair of open/close characters and
        attempt to parse the contents as JSON.
        """
        start = text.find(open_char)
        if start == -1:
            return None

        depth = 0
        end = -1
        for i, ch in enumerate(text[start:], start):
            if ch == open_char:
                depth += 1
            elif ch == close_char:
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        if end == -1:
            return None

        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            return None

    # ── Turn parsing ──────────────────────────────────────────────────────────

    @staticmethod
    def _parse_turn(index: int, item: Any) -> Turn | None:
        """
        Convert a single JSON object into a Turn, tolerating missing keys
        and nested structures.
        """
        if not isinstance(item, dict):
            log.warning("Turn %d is not a dict, skipping", index)
            return None

        # The model sometimes wraps commands under "action", sometimes flat.
        payload = item.get("action") if isinstance(item.get("action"), dict) else item

        speech = _normalise_speech(
            payload.get("speech", item.get("speech", ""))
        )

        raw_cmds = payload.get("commands", item.get("commands", {})) or {}
        if not isinstance(raw_cmds, dict):
            raw_cmds = {}

        commands = Commands(
            pattern   = payload.get("pattern", raw_cmds.get("pattern")),
            speed     = _to_int_or_none(payload.get("speed", raw_cmds.get("speed"))),
            intensity = _to_int_or_none(payload.get("intensity", raw_cmds.get("intensity"))),
            depth     = _to_int_or_none(payload.get("depth", raw_cmds.get("depth"))),
            base      = _to_int_or_none(payload.get("base", raw_cmds.get("base"))),
        )

        return Turn(index=index, speech=speech, commands=commands, raw=item)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_int_or_none(value: Any) -> int | None:
    """Convert a value to int, returning None for null/None/non-numeric."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalise_speech(value: Any) -> str:
    """Convert speech content from either a string or list of strings into text."""
    if isinstance(value, list):
        parts = [str(part).strip() for part in value if part is not None]
        return " ".join(part for part in parts if part)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)