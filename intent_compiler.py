"""
intent_compiler.py
------------------
Maps narrative intents + intensity (0.0-1.0) to concrete device commands.

Directory structure:
    intents/
    ├── tease/
    │   ├── 0.0-0.3.json
    │   ├── 0.3-0.6.json
    │   └── 0.6-1.0.json
    ├── insist/
    │   └── ...

Each JSON defines one intensity band with parameters and optional variations.
"""

import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import INTENTS_DIR

log = logging.getLogger(__name__)


@dataclass
class CompiledCommand:
    """Concrete device command produced by the compiler."""
    pattern: str
    speed: int
    depth: int
    base: int
    ai_intensity: float          # the original (unjittered) intensity from AI
    duration_ms: int
    pattern_intensity: float | None = None  # optional intensity defined in pattern JSON
    easing: str | None = None
    intent: str = ""          # for tracking/debugging

    def as_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "speed": self.speed,
            "depth": self.depth,
            "base": self.base,
            "ai_intensity": self.ai_intensity,
            "pattern_intensity": self.pattern_intensity,
            "duration_ms": self.duration_ms,
            "easing": self.easing,
            "intent": self.intent,
        }


class IntentCompiler:
    """
    Loads intent definitions from disk and compiles (intent, intensity)
    pairs into concrete device commands.
    """

    def __init__(self, intents_dir: Path = INTENTS_DIR):
        self.intents_dir = intents_dir
        self._registry: dict[str, list[dict]] = {}   # intent -> sorted bands
        self._history: list[str] = []                # recent fingerprints
        self._max_history = 10
        self._load_all()

    # ── Public API ───────────────────────────────────────────────────────────

    def compile(
        self,
        intent: str,
        intensity: float,
        jitter: float = 0.0,
    ) -> CompiledCommand:
        """
        Map an intent + intensity to a CompiledCommand.

        Args:
            intent:    The narrative intent (e.g. "tease", "insist")
            intensity: 0.0 to 1.0
            jitter:    ±random offset applied to intensity before band selection
        """
        if intent not in self._registry:
            raise ValueError(
                f"Unknown intent: {intent!r}. "
                f"Known: {list(self._registry.keys())}"
            )
        if intent == "stop":
            return CompiledCommand(
                pattern="stop",
                speed=0,
                depth=None, 
                base=None,
                intensity=0.0,
                duration_ms=5000, 
                intent="stop",
            )

        bands = self._registry[intent]

        # Apply jitter to intensity for band selection
        noisy = max(0.0, min(1.0, intensity + random.uniform(-jitter, jitter)))
        band = self._select_band(bands, noisy)
        cmd = self._render(band, intensity, intent)

        # Avoid exact repetition: if fingerprint matches recent history,
        # retry once with higher jitter
        fingerprint = f"{cmd.pattern}:{cmd.speed}:{cmd.depth}:{cmd.base}"
        if fingerprint in self._history and jitter < 0.15:
            log.debug("Repeat detected, recompiling with jitter=0.15")
            return self.compile(intent, intensity, jitter=0.15)

        self._history.append(fingerprint)
        if len(self._history) > self._max_history:
            self._history.pop(0)

        return cmd

    def list_intents(self) -> list[str]:
        """Return all known intent names."""
        return sorted(self._registry.keys())

    def get_intent_meta(self, intent: str) -> dict[str, Any]:
        """Return metadata about an intent for UI/debugging."""
        bands = self._registry.get(intent, [])
        return {
            "name": intent,
            "bands": len(bands),
            "intensity_coverage": [
                {
                    "min": b["intensity_range"][0],
                    "max": b["intensity_range"][1],
                    "description": b.get("description", ""),
                }
                for b in bands
            ],
        }

    def reload(self) -> None:
        """Reload all intent definitions from disk."""
        self._registry.clear()
        self._history.clear()
        self._load_all()
        log.info("Reloaded %d intent(s)", len(self._registry))

    # ── Internal ─────────────────────────────────────────────────────────────

    def _load_all(self) -> None:
        if not self.intents_dir.exists():
            log.warning("Intents directory not found: %s", self.intents_dir)
            return

        for intent_folder in sorted(self.intents_dir.iterdir()):
            if not intent_folder.is_dir():
                continue

            intent_name = intent_folder.name
            bands: list[dict] = []

            for json_file in sorted(intent_folder.glob("*.json")):
                try:
                    with json_file.open("r", encoding="utf-8") as fh:
                        data = json.load(fh)

                    # Validate
                    if "intensity_range" not in data:
                        log.warning(
                            "%s: missing intensity_range, skipping", json_file
                        )
                        continue
                    rng = data["intensity_range"]
                    if not (isinstance(rng, list) and len(rng) == 2):
                        log.warning(
                            "%s: intensity_range must be [min, max], skipping",
                            json_file,
                        )
                        continue

                    bands.append(data)
                    log.debug("Loaded band %s for intent %s", json_file.name, intent_name)

                except (json.JSONDecodeError, OSError) as exc:
                    log.error("Failed to load %s: %s", json_file, exc)

            if bands:
                bands.sort(key=lambda b: b["intensity_range"][0])
                self._registry[intent_name] = bands
                log.info(
                    "Loaded intent %s with %d band(s)", intent_name, len(bands)
                )

    @staticmethod
    def _select_band(bands: list[dict], intensity: float) -> dict:
        """Pick the band that contains this intensity, or nearest edge."""
        for band in bands:
            lo, hi = band["intensity_range"]
            if lo <= intensity <= hi:
                return band

        # Fallback: clamp to nearest edge
        if intensity < bands[0]["intensity_range"][0]:
            return bands[0]
        return bands[-1]

    @staticmethod
    def _render(band: dict, original_intensity: float, intent: str) -> CompiledCommand:
        """Build a CompiledCommand from a band, applying variations if present."""
        params = band.copy()

        # Weighted random variation selection
        if "variations" in band and isinstance(band["variations"], list):
            weights = [
                v.get("probability", 1.0) for v in band["variations"]
            ]
            total = sum(weights)
            if total > 0:
                r = random.uniform(0, total)
                cumulative = 0.0
                for var in band["variations"]:
                    cumulative += var.get("probability", 1.0)
                    if r <= cumulative:
                        params.update(var)
                        break

        # Pattern-level intensity (optional) may be present in the band/variation.
        pattern_intensity = None
        if "intensity" in params:
            try:
                pattern_intensity = float(params.get("intensity"))
            except (TypeError, ValueError):
                pattern_intensity = None

        return CompiledCommand(
            pattern=params.get("pattern", "simple_stroke"),
            speed=params.get("speed", 50),
            depth=params.get("depth", 50),
            base=params.get("base", 0),
            ai_intensity=original_intensity,
            pattern_intensity=pattern_intensity,
            duration_ms=params.get("duration_ms", 5000),
            easing=params.get("easing"),
            intent=intent,
        )