"""
prompt_builder.py
-----------------
Constructs lightweight user prompts for stateful chat sessions.

The system prompt is sent once at session start. Each turn only
sends fresh context: recent speech, current state, and task.
"""

import logging
import random
import re
from pathlib import Path
from typing import Any

from config import (
    BANNED_PHRASE_WINDOW,
    DEVICE_PROMPT_FALLBACK,
    EXAMPLES_DIR,
    PACING_STRATEGIES_FILE,
    PERSONA_MOODS_FILE,
    PROMPT_DEVICES_DIR,
    PROMPT_FILE,
    USER_TURN_TASK_FILE,
)
from feedback_store import load_banned_phrases
from pattern_loader import PatternLoader
from prompt_store import resolve_prompt_path
from response_parser import Turn
from session_manager import DeviceState

log = logging.getLogger(__name__)

# How the user's reaction to a recent line is described back to the model.
# Reacted lines are shown in full (no truncation) so the model can emulate or
# avoid them precisely. Banned lines are handled separately and permanently.
_REACTION_NOTES = {
    "love": "❤️ the user LOVED this — stay in this register and build on it",
    "like": "👍 the user liked this — more in this direction",
    "dislike": "👎 the user disliked this — steer away from this style/direction",
}

# Marks an optional section in a prompt template: kept when the feature is on,
# stripped entirely (markers and content) when it is off.
_VIDEO_BLOCK = re.compile(r"\{\{#VIDEO\}\}(.*?)\{\{/VIDEO\}\}", re.DOTALL)


def _read_nonempty_lines(path: Path) -> list[str]:
    resolved = resolve_prompt_path(path)
    if not resolved.exists():
        log.warning("Prompt list file not found: %s", resolved)
        return []
    try:
        return [
            line.strip()
            for line in resolved.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except OSError as exc:
        log.warning("Could not read prompt list %s: %s", resolved, exc)
        return []


def _read_text_file(path: Path) -> str:
    resolved = resolve_prompt_path(path)
    if not resolved.exists():
        log.warning("Prompt text file not found: %s", resolved)
        return ""
    try:
        return resolved.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log.warning("Could not read prompt text %s: %s", resolved, exc)
        return ""


# Drivers that differ only in transport, not in what the hardware can do, share
# one device block. Keeping the alias here rather than duplicating the file
# stops the two copies drifting apart.
DEVICE_PROMPT_ALIASES = {
    "ossm_ble": "ossm",
}


def get_device_block(device_type: str | None) -> str:
    """Return the instruction block describing ``device_type``.

    Falls back to the generic block when the type is unknown or has no file of
    its own, so a newly added driver still produces a coherent prompt instead of
    inheriting the previous device's physical vocabulary.
    """
    name = (device_type or DEVICE_PROMPT_FALLBACK).strip().lower()
    name = DEVICE_PROMPT_ALIASES.get(name, name)
    block = _read_text_file(PROMPT_DEVICES_DIR / f"{name}.txt")
    if not block and name != DEVICE_PROMPT_FALLBACK:
        log.warning("No device prompt for %r; using %s", name, DEVICE_PROMPT_FALLBACK)
        block = _read_text_file(PROMPT_DEVICES_DIR / f"{DEVICE_PROMPT_FALLBACK}.txt")
    return block


def get_persona_moods() -> list[str]:
    return _read_nonempty_lines(PERSONA_MOODS_FILE)


def get_pacing_strategies() -> list[str]:
    return _read_nonempty_lines(PACING_STRATEGIES_FILE)


class PromptBuilder:
    """
    Builds prompts for stateful chat sessions.

    System prompt is loaded once and sent at session start.
    User prompts are minimal: only fresh context per turn.
    """

    def __init__(self):
        self.pattern_loader = PatternLoader()
        self.banned_phrase_window = BANNED_PHRASE_WINDOW
        self.reload()

        self.current_persona: str | None = None
        self.current_pacing: str | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    def get_system_prompt(
        self,
        video_enabled: bool = True,
        device_type: str | None = None,
    ) -> str:
        """
        Return the full system prompt. Call this once at session start
        and pass it to connector.start_session().

        When ``video_enabled`` is False the play_video intent is dropped from
        the prompt so the AI is never offered it.

        ``device_type`` selects the DEVICE PROFILE block (see get_device_block);
        it is the registry's active device id, not a display name.
        """
        return self._build_system_prompt(video_enabled, device_type)

    def build_user_prompt(
        self,
        session_turns: list[Turn],
        n_turns: int,
        device_state: DeviceState | None = None,
        user_event: str | None = None,
        selected_persona: str | None = None,
        selected_pacing: str | None = None,
    ) -> str:
        """
        Build a minimal user prompt for a stateful session.
        Only includes fresh context that changes each turn.
        """
        sections: list[str] = []

        # A) User event (highest priority — react immediately)
        if user_event:
            sections.append(f"USER EVENT: {user_event}")

        # B) Current device state (so model knows what's happening)
        if device_state:
            state_desc = self._format_device_state(device_state)
            sections.append(f"Current state: {state_desc}")

        # C) Recent speech (last N turns) — used both to avoid repetition and to
        # surface the user's reactions. Unreacted lines are truncated to save
        # tokens; lines the user reacted to are shown in full and annotated so
        # the model can emulate (like/love) or avoid (dislike) them precisely.
        recent_turns = session_turns[-self.banned_phrase_window:]
        if recent_turns:
            lines = []
            for t in recent_turns:
                note = _REACTION_NOTES.get(getattr(t, "reaction", None))
                if note:
                    lines.append(f'  - "{t.speech}"  →  {note}')
                else:
                    lines.append(f'  - "{t.speech[:50]}..."')
            sections.append(
                "Recent speech (do NOT repeat verbatim). Where a line shows the "
                "user's reaction, weight it strongly:\n" + "\n".join(lines)
            )

        # C2) Persistent banned phrases (🚫) — hard constraint, never expires.
        banned_phrases = load_banned_phrases()
        if banned_phrases:
            blocked = "\n".join(f'  - "{p}"' for p in banned_phrases)
            sections.append(
                "BANNED — never say these lines again, or anything close to them:\n"
                + blocked
            )

        # D) Session continuity hint (only after many turns)
        if len(session_turns) >= 15:
            sections.append(
                "Continue the session. Escalate or twist what happened earlier. "
                "Do NOT restart with a fresh introduction."
            )

        # E) Task instruction
        if self._user_turn_task_template:
            sections.append(self._user_turn_task_template.format(n_turns=n_turns))

        return "\n\n".join(sections) if sections else "Continue."

    def build_session_seed_prompt(
        self,
        selected_persona: str | None = None,
        selected_pacing: str | None = None,
    ) -> str:
        """
        Build the FIRST user message that sets up the session.
        This includes persona, pacing, and opening pattern — sent once.
        """
        sections: list[str] = []

        # A) Session plan (pacing strategy)
        pacing = _pick_or_random(selected_pacing, self._pacing_strategies)
        self.current_pacing = pacing
        sections.append(
            f"You MUST use this pacing strategy for this session: {pacing}"
        )

        # B) Persona mood
        mood = _pick_or_random(selected_persona, self._persona_moods)
        self.current_persona = mood
        sections.append(f"Your dominant affect: {mood}")

        # C) Opening intent (instead of pattern)
        opening_intent = _pick_or_random(None, self._opening_intents)
        sections.append(f'Start with intent: "{opening_intent}".')

        # D) First turn instruction
        sections.append(
            "The device is stopped and idle. The User is ready. "
            "Generate the first 5 outputs and actions."
        )

        return "\n\n".join(sections)

    # ── System prompt ─────────────────────────────────────────────────────────

    def _build_system_prompt(
        self,
        video_enabled: bool = True,
        device_type: str | None = None,
    ) -> str:
        pattern_block = self.pattern_loader.to_prompt_block()
        prompt = self._base_prompt

        if "{{PATTERNS_BLOCK}}" in prompt:
            prompt = prompt.replace("{{PATTERNS_BLOCK}}", pattern_block)

        if "{{DEVICE_BLOCK}}" in prompt:
            device_block = get_device_block(device_type)
            prompt = prompt.replace("{{DEVICE_BLOCK}}", device_block)
            log.info(
                "System prompt built for device %r (%d chars of profile)",
                device_type or DEVICE_PROMPT_FALLBACK,
                len(device_block),
            )

        if "{{EXAMPLES_BLOCK}}" in prompt:
            prompt = prompt.replace("{{EXAMPLES_BLOCK}}", "\n\n".join(self._examples))

        if video_enabled:
            prompt = prompt.replace("{{#VIDEO}}", "").replace("{{/VIDEO}}", "")
        else:
            prompt = _VIDEO_BLOCK.sub("", prompt)

        return prompt

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _format_device_state(state: DeviceState) -> str:
        parts = []
        if getattr(state, 'intent', None):
            parts.append(f"intent={state.intent}")
        if getattr(state, 'intensity', None) is not None:
            parts.append(f"intensity={state.intensity}")
        if state.pattern:
            parts.append(f"pattern={state.pattern}")
        if state.speed is not None:
            parts.append(f"speed={state.speed}")
        if state.depth is not None:
            parts.append(f"depth={state.depth}")
        if state.base is not None:
            parts.append(f"base={state.base}")
        return ", ".join(parts) if parts else "stopped"

    # ── File loading ──────────────────────────────────────────────────────────

    @staticmethod
    def _load_base_prompt() -> str:
        resolved = resolve_prompt_path(PROMPT_FILE)
        if not resolved.exists():
            log.warning("Base prompt file not found: %s", resolved)
            return ""
        text = resolved.read_text(encoding="utf-8")
        log.info("Loaded base prompt (%d chars)", len(text))
        return text

    @staticmethod
    def _load_examples() -> list[str]:
        examples: list[str] = []
        if not EXAMPLES_DIR.exists():
            return examples

        for path in sorted(EXAMPLES_DIR.glob("*.*")):
            if path.suffix.lower() not in {".txt", ".json"}:
                continue
            try:
                resolved = resolve_prompt_path(path)
                examples.append(resolved.read_text(encoding="utf-8"))
                log.debug("Loaded example: %s", resolved.name)
            except OSError as exc:
                log.warning("Could not load example %s: %s", path, exc)

        log.info("Loaded %d example file(s)", len(examples))
        return examples

    def reload(self) -> None:
        self._base_prompt = self._load_base_prompt()
        self._examples = self._load_examples()
        self._opening_intents = get_opening_intents()
        self._persona_moods = get_persona_moods()
        self._pacing_strategies = get_pacing_strategies()

        self._user_turn_task_template = _read_text_file(USER_TURN_TASK_FILE)


def _pick_or_random(selected: str | None, available: list[str]) -> str:
    if selected and selected.lower() != "random" and selected in available:
        return selected
    if available:
        return random.choice(available)
    return ""   

def get_opening_intents() -> list[str]:
    # Must be real intents (an intents/<name>/ dir, or the built-in "stop");
    # "ground" had no definition and silently failed to compile.
    return ["tease", "settle", "stop"]