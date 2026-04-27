"""
pattern_loader.py
-----------------
Loads pattern definitions from the patterns/ directory.
Each .json file in that directory describes one motion pattern.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Any

from config import PATTERNS_DIR

log = logging.getLogger(__name__)


class PatternLoader:
    """
    Reads all *.json files from the patterns directory and exposes them
    as a dict keyed by pattern name (filename without extension).
    """

    def __init__(self, patterns_dir: Path = PATTERNS_DIR):
        self.patterns_dir = patterns_dir
        self._patterns: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def all(self) -> Dict[str, Dict[str, Any]]:
        """Return all loaded patterns."""
        return dict(self._patterns)

    def names(self) -> list[str]:
        """Return sorted list of pattern names."""
        return sorted(self._patterns.keys())

    def get(self, name: str) -> Dict[str, Any] | None:
        """Return a single pattern by name, or None if not found."""
        return self._patterns.get(name)

    def to_prompt_block(self) -> str:
        """
        Serialise all patterns into the JSON blob used inside the system prompt.
        Mirrors the format expected by the AI persona.
        """
        lines = ["{"]
        entries = list(self._patterns.items())
        for i, (name, definition) in enumerate(entries):
            comma = "," if i < len(entries) - 1 else ""
            lines.append(f"{name}:{json.dumps(definition, indent=2)}{comma}")
        lines.append("}")
        return "\n".join(lines)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.patterns_dir.exists():
            log.warning("Patterns directory not found: %s", self.patterns_dir)
            return

        for path in sorted(self.patterns_dir.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                name = path.stem
                self._patterns[name] = data
                log.debug("Loaded pattern: %s", name)
            except (json.JSONDecodeError, OSError) as exc:
                log.error("Failed to load pattern %s: %s", path, exc)

        log.info("Loaded %d pattern(s) from %s", len(self._patterns), self.patterns_dir)