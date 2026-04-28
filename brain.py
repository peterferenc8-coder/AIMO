"""
brain.py
--------
The Brain is the creative director of the application.

Manages session state and coordinates prompt generation for stateful
chat sessions. The system prompt is sent once at session start; 
subsequent turns only send minimal user prompts.
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
        system_prompt = brain.get_system_prompt()
        connector.start_session(system_prompt)
        
        seed_prompt = brain.build_seed_prompt(persona="...", pacing="...")
        connector.send_message(seed_prompt)
        
        # ... later ...
        user_prompt = brain.build_turn_prompt(n_turns=5, device_state=...)
        connector.send_message(user_prompt)
    """

    def __init__(self):
        self._prompt_builder = PromptBuilder()
        self.session_turns: list[Turn] = []
        self._session_started: bool = False

    # ── Public API ────────────────────────────────────────────────────────────

    def get_system_prompt(self) -> str:
        """Return the full system prompt. Call once at session start."""
        return self._prompt_builder.get_system_prompt()

    def build_seed_prompt(
        self,
        selected_persona: str | None = None,
        selected_pacing: str | None = None,
    ) -> str:
        """
        Build the FIRST user message that sets up the session.
        Includes persona, pacing, and opening pattern — sent once.
        """
        return self._prompt_builder.build_session_seed_prompt(
            selected_persona=selected_persona,
            selected_pacing=selected_pacing,
        )

    def build_turn_prompt(
        self,
        n_turns: int = DEFAULT_TURNS,
        device_state=None,
        user_event: str | None = None,
    ) -> str:
        """
        Build a minimal user prompt for a stateful session.
        Only includes fresh context that changes each turn.
        """
        return self._prompt_builder.build_user_prompt(
            session_turns=self.session_turns,
            n_turns=n_turns,
            device_state=device_state,
            user_event=user_event,
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
        self._session_started = False
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