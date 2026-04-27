"""
brain.py
--------
The Brain is the creative director of the application.

It holds session state (which turns have happened, what was said) and
orchestrates prompt construction via PromptBuilder.
"""

import logging

from config import DEFAULT_TURNS
from prompt_builder import PromptBuilder
from response_parser import Turn

log = logging.getLogger(__name__)


class Brain:
    """
    Manages session state and coordinates prompt generation.

    Typical lifecycle::

        brain = Brain()
        prompt_data = brain.build_prompt(n_turns=5)
        # … call AI …
        brain.record_turns(parsed_turns)
    """

    def __init__(self):
        self._prompt_builder = PromptBuilder()
        self.session_turns: list[Turn] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def build_prompt(
        self,
        n_turns: int = DEFAULT_TURNS,
        selected_persona: str | None = None,
        selected_pacing: str | None = None,
    ) -> dict[str, str]:
        """
        Return prompt data ready to send to the model and display to the user.

        The returned dict contains:
            - system_prompt: full persona/rules document
            - user_prompt: diversity seeds + session history
            - persona: the mood selected for this generation
            - pacing: the pacing strategy selected for this generation
        """
        return self._prompt_builder.build(
            session_turns=self.session_turns,
            n_turns=n_turns,
            selected_persona=selected_persona,
            selected_pacing=selected_pacing,
        )

    def record_turns(self, turns: list[Turn]) -> None:
        """
        Persist parsed turns into session history so future prompts can
        reference them as banned phrases.
        """
        self.session_turns.extend(turns)
        log.info(
            "Recorded %d new turn(s). Total session turns: %d",
            len(turns),
            len(self.session_turns),
        )

    def clear_session(self) -> None:
        """Reset session history (start fresh)."""
        self.session_turns = []
        log.info("Session cleared.")

    def session_summary(self) -> dict:
        """Lightweight summary for the UI's status panel."""
        return {
            "total_turns": len(self.session_turns),
            "patterns_available": self._prompt_builder.pattern_loader.names(),
        }

    # ── Proxied properties ────────────────────────────────────────────────────

    @property
    def pattern_loader(self):
        """Expose the pattern loader so routes can list pattern names."""
        return self._prompt_builder.pattern_loader

    @property
    def current_persona(self) -> str | None:
        """The mood used in the most recent build_prompt() call."""
        return self._prompt_builder.current_persona

    @property
    def current_pacing(self) -> str | None:
        """The pacing strategy used in the most recent build_prompt() call."""
        return self._prompt_builder.current_pacing