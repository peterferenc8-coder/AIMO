"""
prompt_builder.py
-----------------
Constructs prompts for both the heavy big model and the lightweight
small filler model.
"""

import logging
import random
from pathlib import Path
from typing import Any

from config import (
    BANNED_PHRASE_WINDOW,
    EXAMPLES_DIR,
    OPENING_PATTERNS_FILE,
    PACING_STRATEGIES_FILE,
    PERSONA_MOODS_FILE,
    PROMPT_FILE,
    SMALL_EXAMPLES_DIR,
    SMALL_FALLBACK_TASK_FILE,
    SMALL_PROMPT_FILE,
    SMALL_STATE_MOVING_FILE,
    SMALL_STATE_STOPPED_FILE,
    USER_TURN_TASK_FILE,
)
from pattern_loader import PatternLoader
from prompt_store import resolve_prompt_path
from response_parser import Turn
from session_manager import DeviceState

log = logging.getLogger(__name__)


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


def get_persona_moods() -> list[str]:
    return _read_nonempty_lines(PERSONA_MOODS_FILE)


def get_pacing_strategies() -> list[str]:
    return _read_nonempty_lines(PACING_STRATEGIES_FILE)


def get_opening_patterns() -> list[str]:
    return _read_nonempty_lines(OPENING_PATTERNS_FILE)


class PromptBuilder:
    """
    Builds system and user prompts for each generation request.
    """

    def __init__(self):
        self.pattern_loader = PatternLoader()
        self.reload()

        # These are set per-build so the UI can display what was used.
        self.current_persona: str | None = None
        self.current_pacing: str | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    def build(
        self,
        session_turns: list[Turn],
        n_turns: int,
        selected_persona: str | None = None,
        selected_pacing: str | None = None,
    ) -> dict[str, str]:
        """
        Return a dict with system_prompt, user_prompt, persona, and pacing.
        """
        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(
            session_turns=session_turns,
            n_turns=n_turns,
            selected_persona=selected_persona,
            selected_pacing=selected_pacing,
        )

        return {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "persona": self.current_persona,
            "pacing": self.current_pacing,
        }

    def build_small_prompt(
        self,
        session_turns: list[Turn],
        device_state: DeviceState,
        persona: str | None = None,
    ) -> str:
        """
        Build a lightweight prompt for the fast filler model.
        Returns raw text — the model should output speech only.
        """
        # ── Load template ─────────────────────────────────────────────────────
        base = _read_text_file(SMALL_PROMPT_FILE)

        # ── Persona ───────────────────────────────────────────────────────────
        mood = persona or self.current_persona or _pick_or_random(None, self._persona_moods)

        # ── State description in natural language ─────────────────────────────
        pattern = device_state.pattern or "stop"
        speed = device_state.speed or 0
        depth = device_state.depth or 0
        base_pos = device_state.base or 0
        intensity = device_state.intensity or 0

        if pattern == "stop":
            state_desc = self._small_state_stopped_template.format(depth=depth)
        else:
            state_desc = self._small_state_moving_template.format(
                pattern=pattern,
                speed=speed,
                depth=depth,
                base=base_pos,
                intensity=intensity,
            )

        # ── Recent speech (so it doesn't repeat) ──────────────────────────────
        recent = [t.speech for t in session_turns[-5:]]
        recent_block = ""
        if recent:
            lines = "\n".join(f'  - "{s[:150]}"' for s in recent)
            recent_block = (
                f"== RECENT SPEECH (do NOT imitate or repeat these ideas) ==\n"
                f"{lines}"
            )

        recent_endings = []
        for s in recent:
            words = s.split()
            if len(words) >= 2:
                recent_endings.append(" ".join(words[-2:]))  # last 2 words
            if len(words) >= 4:
                recent_endings.append(" ".join(words[-4:]))  # last 4 words

        if recent_endings:
            endings_lines = "\n".join(f'  - "{e}"' for e in set(recent_endings))
            recent_block += (
                f"\n\n== BANNED ENDINGS & CONCEPTS (do NOT use these closing phrases or ideas) ==\n"
                f"{endings_lines}"
    )

        # ── Few-shot style injection (rotating examples keep it fresh) ────────
        # Pick 2 random examples so the model doesn't memorize one
        chosen_examples = random.sample(
            self._small_examples, min(2, len(self._small_examples))
        ) if self._small_examples else []
        examples_block = "\n\n".join(
            f"== EXAMPLE ==\n{e}" for e in chosen_examples
        )

        context = (
            f"== PERSONA MOOD ==\n{mood}\n\n"
            f"== CURRENT DEVICE STATE ==\n{state_desc}\n\n"
            f"{recent_block}\n\n"
            f"{examples_block}"
        )

        # ── Assemble ──────────────────────────────────────────────────────────
        if base and "{{CONTEXT_BLOCK}}" in base:
            prompt = base.replace("{{CONTEXT_BLOCK}}", context)
        elif base:
            prompt = base + "\n\n" + context
        else:
            prompt = context
            if self._small_fallback_task:
                prompt += "\n\n" + self._small_fallback_task

        return prompt

    # ── System prompt ─────────────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        """
        Substitute the live pattern block into the base prompt template.
        """
        pattern_block = self.pattern_loader.to_prompt_block()
        prompt = self._base_prompt

        if "{{PATTERNS_BLOCK}}" in prompt:
            prompt = prompt.replace("{{PATTERNS_BLOCK}}", pattern_block)

        return prompt

    # ── User prompt ───────────────────────────────────────────────────────────

    def _build_user_prompt(
        self,
        session_turns: list[Turn],
        n_turns: int,
        selected_persona: str | None,
        selected_pacing: str | None,
    ) -> str:
        """
        Assemble the user message that carries all diversity seeds.
        """
        sections: list[str] = []

        # A) Session plan (pacing strategy)
        pacing = _pick_or_random(selected_pacing, self._pacing_strategies)
        self.current_pacing = pacing
        sections.append(
            f"== THIS SESSION PLAN ==\n"
            f"You MUST use this pacing strategy for this session:\n"
            f"  {pacing}\n"
            f"Commit to it from turn 1. Do not drift to a different strategy."
        )

        # B) Persona mood
        mood = _pick_or_random(selected_persona, self._persona_moods)
        self.current_persona = mood
        sections.append(
            f"== PERSONA MOOD THIS SESSION ==\n"
            f"Your dominant affect for every line of speech:\n"
            f"  {mood}"
        )

        # C) Opening pattern
        opening = _pick_or_random(None, self._opening_patterns)
        sections.append(
            f"== OPENING PATTERN ==\n"
            f'Start your FIRST turn with pattern: "{opening}".\n'
            f"Do NOT open with simple_stroke unless that is what is listed above."
        )

        # D) Banned phrases (recent speech to avoid repetition)
        recent_speech = [
            t.speech for t in session_turns[-BANNED_PHRASE_WINDOW:]
        ]
        if recent_speech:
            banned_block = "\n".join(f'  - "{s}"' for s in recent_speech)
            sections.append(
                f"== BANNED PHRASES (already used — never repeat) ==\n"
                f"{banned_block}"
            )

        # E) Previous answer context (continuity)
        previous_answer = self._build_previous_answer_block(session_turns)
        if previous_answer:
            sections.append(previous_answer)

        # F) Few-shot style example
        if self._examples:
            example_text = random.choice(self._examples)
            sections.append(
                f"== STYLE REFERENCE EXAMPLE (do NOT copy — use for style only) ==\n"
                f"{example_text}"
            )

        # G) Final task instruction
        if self._user_turn_task_template:
            sections.append(self._user_turn_task_template.format(n_turns=n_turns))

        return "\n\n".join(sections)

    def _build_previous_answer_block(
        self, session_turns: list[Turn], max_lines: int = 30
    ) -> str | None:
        """
        Build a compact context block from the most recent assistant output
        so the model continues instead of restarting.
        """
        if not session_turns:
            return None

        last_turn = session_turns[-1]

        # Collect all non-empty lines from the entire session history.
        lines: list[str] = []
        for turn in session_turns:
            speech = turn.speech.strip()
            if not speech:
                continue
            lines.extend(line for line in speech.splitlines() if line.strip())

        if not lines:
            return None

        previous_lines = lines[-max_lines:]
        previous_text = "\n".join(previous_lines)

        last_commands = last_turn.commands.as_dict()

        cycle_hint = ""
        if len(session_turns) >= 15:
            cycle_hint = (
                "\n\nThis is NOT the beginning of the session. "
                "Do NOT restart with a fresh opener or introduction. "
                "Acknowledge that you have done this before and are now escalating, repeating, or twisting what happened earlier. "
                "Reference the Subject's previous reactions if relevant."
            )

        return (
            f"== PREVIOUS ANSWER ==\n"
            f"This is the most recent assistant output from earlier in the session.\n"
            f"Continue from this exact point instead of restarting a fresh opener.\n"
            f"Last turn state: pattern={last_turn.raw.get('action', {}).get('pattern')}, "
            f"speed={last_commands['speed']}, intensity={last_commands['intensity']}, "
            f"depth={last_commands['depth']}, base={last_commands['base']}\n"
            f"Use it as continuity context and avoid repeating phrases if you can.\n"
            f"{previous_text}"
            f"{cycle_hint}"

        )

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
        """
        Load any .txt or .json files from the examples/ directory.
        These are injected randomly as few-shot style references.
        """
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

    @staticmethod
    def _load_small_examples() -> list[str]:
        examples: list[str] = []
        if not SMALL_EXAMPLES_DIR.exists():
            return examples

        for path in sorted(SMALL_EXAMPLES_DIR.glob("*.txt")):
            try:
                resolved = resolve_prompt_path(path)
                text = resolved.read_text(encoding="utf-8").strip()
                if text:
                    examples.append(text)
            except OSError as exc:
                log.warning("Could not load small example %s: %s", path, exc)

        log.info("Loaded %d small example file(s)", len(examples))
        return examples

    def reload(self) -> None:
        self._base_prompt = self._load_base_prompt()
        self._examples = self._load_examples()
        self._small_examples = self._load_small_examples()

        self._persona_moods = get_persona_moods()
        self._pacing_strategies = get_pacing_strategies()
        self._opening_patterns = get_opening_patterns()

        self._small_state_stopped_template = _read_text_file(SMALL_STATE_STOPPED_FILE)
        self._small_state_moving_template = _read_text_file(SMALL_STATE_MOVING_FILE)
        self._user_turn_task_template = _read_text_file(USER_TURN_TASK_FILE)
        self._small_fallback_task = _read_text_file(SMALL_FALLBACK_TASK_FILE)


def _pick_or_random(selected: str | None, available: list[str]) -> str:
    """Return the user-selected item if valid; otherwise pick at random."""
    if selected and selected.lower() != "random" and selected in available:
        return selected
    if available:
        return random.choice(available)
    return ""