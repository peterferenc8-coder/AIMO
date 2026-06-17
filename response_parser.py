"""
response_parser.py
------------------
Parses the raw text coming back from the model and extracts structured
fields: speech, intent, intensity, duration_ms.

The model returns a JSON list of objects with intent+intensity format.
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
    Parsed device command block — now produced by IntentCompiler, not the LLM.
    Kept for backward compatibility during transition.
    """
    pattern:   str | None = None
    speed:     int | None = None
    intensity: float | None = None
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
    """One complete model turn: what was said + what the AI wants to do."""
    index:     int
    speech:    str
    intent:    str | None = None       # NEW: narrative intent
    ai_intensity: float | None = None    # NEW: 0.0-1.0 (from the AI)
    duration_ms: int | None = None     # NEW: how long this intent lasts
    commands:  Commands = field(default_factory=Commands)
    raw:       dict = field(default_factory=dict)
    # User reaction from the UI feedback buttons: "like" | "love" | "dislike"
    # | "ban" | None. Transient (lives with the session); bans are also
    # persisted separately via feedback_store.
    reaction:  str | None = None

    def as_dict(self) -> dict:
        return {
            "index":     self.index,
            "speech":    self.speech,
            "intent":    self.intent,
            "ai_intensity": self.ai_intensity,
            "duration_ms": self.duration_ms,
            "commands":  self.commands.as_dict(),
            "raw":       self.raw,
            "reaction":  self.reaction,
        }


# ── Parser ────────────────────────────────────────────────────────────────────

class ResponseParser:
    """
    Converts a raw model response string into a list of Turn objects.
    Expects intent+intensity format from the LLM.
    """

    def parse(self, raw_text: str) -> list[Turn]:
        cleaned = self._strip_markdown_fences(raw_text)
        payload = self._extract_json(cleaned)

        if payload is None:
            log.error("Could not extract JSON from model response")
            return []

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

    # ── JSON extraction (unchanged) ─────────────────────────────────────────

    @staticmethod
    def _strip_markdown_fences(text: str) -> str:
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return text.strip()

    def _extract_json(self, text: str) -> Any:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        ndjson = self._extract_ndjson(text)
        if ndjson is not None:
            return ndjson

        list_payload = self._extract_balanced(text, "[", "]")
        if list_payload is not None:
            return list_payload

        obj_payload = self._extract_balanced(text, "{", "}")
        if obj_payload is not None:
            return obj_payload

        return None

    def _extract_ndjson(self, text: str) -> list[dict] | None:
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

    # ── Turn parsing (updated for intent+intensity) ─────────────────────────

    @staticmethod
    def _parse_turn(index: int, item: Any) -> Turn | None:
        if not isinstance(item, dict):
            log.warning("Turn %d is not a dict, skipping", index)
            return None

        # Support nested "action" wrapper or flat structure
        payload = item.get("action") if isinstance(item.get("action"), dict) else item

        speech = _normalise_speech(
            payload.get("speech", item.get("speech", ""))
        )

        # NEW: Extract intent + ai_intensity + duration
        intent = _normalise_string(payload.get("intent"))
        # Accept new key `ai_intensity` but fall back to legacy `intensity`.
        ai_intensity = _to_float_or_none(
            payload.get("ai_intensity", payload.get("intensity"))
        )
        duration_ms = _to_int_or_none(payload.get("duration_ms"))

        # Legacy commands block (optional, for backward compat)
        raw_cmds = payload.get("commands", item.get("commands", {})) or {}
        if not isinstance(raw_cmds, dict):
            raw_cmds = {}

        commands = Commands(
            pattern   = payload.get("pattern", raw_cmds.get("pattern")),
            speed     = _to_int_or_none(payload.get("speed", raw_cmds.get("speed"))),
            intensity = _to_float_or_none(payload.get("intensity", raw_cmds.get("intensity"))),
            depth     = _to_int_or_none(payload.get("depth", raw_cmds.get("depth"))),
            base      = _to_int_or_none(payload.get("base", raw_cmds.get("base"))),
        )

        return Turn(
            index=index,
            speech=speech,
            intent=intent,
            ai_intensity=ai_intensity,
            duration_ms=duration_ms,
            commands=commands,
            raw=item,
        )


# ── Helpers ─────────────────────────────────────────────────────────────────

def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        return max(0.0, min(1.0, f))  # Clamp to [0, 1]
    except (TypeError, ValueError):
        return None


def _normalise_speech(value: Any) -> str:
    if isinstance(value, list):
        parts = [str(part).strip() for part in value if part is not None]
        return " ".join(part for part in parts if part)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _normalise_string(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip().lower()
    return s if s else None