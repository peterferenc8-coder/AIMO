"""
orchestrator.py
---------------
Manages the dual-model generation flow:
  - Big model: slow, full turns with commands (buffer generation)
  - Small model: fast, speech-only filler while big model warms up

Handles timing, pause/resume, and the display queue.
"""

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from device_bridge import get_bridge

from config import (
    DEFAULT_TURNS,
    GROQ_MODEL_OPTIONS,
    MODEL_OPTIONS,
    SMALL_MODEL,
    SMALL_MODEL_MAX_INTERVAL,
    SMALL_MODEL_MIN_INTERVAL,
)
from ai_connector import GoogleAIConnector, GroqAIConnector
from brain import Brain
from prompt_builder import PromptBuilder
from response_parser import Commands, ResponseParser, Turn
from session_manager import SessionManager
from settings_store import load_settings

log = logging.getLogger(__name__)


@dataclass
class DisplayItem:
    source: str  # "small" | "big"
    speech: str
    commands: dict = field(default_factory=dict)
    raw: Any = None
    index: int = 0

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "speech": self.speech,
            "commands": self.commands,
            "raw": self.raw,
            "index": self.index,
        }


class SessionOrchestrator:
    """
    Coordinates big-model buffering with small-model filler speech.
    """

    def __init__(self):
        self._settings = load_settings()
        self.google_connector = GoogleAIConnector(
            api_key=self._settings.get("google_api_key", ""),
            model=self._settings.get("google_model", "gemma-4-31b-it"),
        )
        self.groq_connector = GroqAIConnector(
            api_key=self._settings.get("groq_api_key", ""),
            model=self._settings.get("groq_model", "openai/gpt-oss-120b"),
        )

        default_model = self._settings.get("google_model", self.google_connector.model)
        self.big_connector = self._connector_for_model(default_model)
        self.big_connector.model = default_model

        self.small_connector = GoogleAIConnector(
            api_key=self._settings.get("google_api_key", ""),
            model=SMALL_MODEL,
        )

        self.brain = Brain()
        self.parser = ResponseParser()
        self.session = SessionManager()
        self.prompt_builder = PromptBuilder()

        self.lock = threading.Lock()

        self.state = "idle"

        self._pending: list[DisplayItem] = []
        self._displayed: list[DisplayItem] = []
        self._display_index = 0

        self._last_display_time = 0.0
        self._next_interval = 15.0

        self._small_timer: threading.Timer | None = None
        self._big_thread: threading.Thread | None = None
        self._small_in_flight = False

        self._n_turns = DEFAULT_TURNS
        self._persona: str | None = None
        self._pacing: str | None = None

        self.device_bridge = get_bridge()

    def apply_settings(self, settings: dict[str, str]) -> dict[str, str]:
        """Update live connectors and prompt assets from saved settings."""
        self._settings.update(settings)
        self.google_connector.reconfigure(
            api_key=self._settings.get("google_api_key", ""),
            model=self._settings.get("google_model", self.google_connector.model),
        )
        self.groq_connector.reconfigure(
            api_key=self._settings.get("groq_api_key", ""),
            model=self._settings.get("groq_model", self.groq_connector.model),
        )
        self.small_connector.reconfigure(
            api_key=self._settings.get("google_api_key", ""),
            model=SMALL_MODEL,
        )

        active_model = self.big_connector.model
        self.big_connector = self._connector_for_model(active_model)

        self.prompt_builder.reload()
        return self._settings

    def reload_prompts(self) -> None:
        self.prompt_builder.reload()

    def start(
        self,
        n_turns: int = DEFAULT_TURNS,
        persona: str | None = None,
        pacing: str | None = None,
        model: str | None = None,
    ) -> dict:
        from prompt_builder import (
            _pick_or_random,
            get_pacing_strategies,
            get_persona_moods,
        )

        resolved_persona = _pick_or_random(persona, get_persona_moods())
        resolved_pacing = _pick_or_random(pacing, get_pacing_strategies())

        with self.lock:
            self.state = "running"
            self._n_turns = n_turns
            self._persona = resolved_persona
            self._pacing = resolved_pacing
            self._pending.clear()
            self._displayed.clear()
            self._display_index = 0
            self._last_display_time = time.time()
            self._next_interval = random.randint(
                SMALL_MODEL_MIN_INTERVAL, SMALL_MODEL_MAX_INTERVAL
            )

            self.session.clear()
            self.brain.clear_session()

            if model:
                self.big_connector = self._connector_for_model(model)
                self.big_connector.model = model

        log.info(
            "Session started  turns=%d  persona=%s  pacing=%s  big_model=%s",
            n_turns,
            resolved_persona,
            resolved_pacing,
            self.big_connector.model,
        )

        self._schedule_small()
        self._request_big_model()

        return self.status

    def pause(self) -> dict:
        with self.lock:
            if self.state == "running":
                self.state = "paused"
                self._cancel_small_timer()
                log.info("Session paused")
        return self.status

    def resume(self) -> dict:
        with self.lock:
            if self.state == "paused":
                self.state = "running"
                self._last_display_time = time.time()
                self._next_interval = random.randint(
                    SMALL_MODEL_MIN_INTERVAL, SMALL_MODEL_MAX_INTERVAL
                )
                log.info("Session resumed")

        with self.lock:
            has_big_pending = any(i.source == "big" for i in self._pending)
            has_big_in_flight = (
                self._big_thread is not None and self._big_thread.is_alive()
            )

        if not has_big_pending and not has_big_in_flight:
            self._schedule_small()

        return self.status

    def clear(self) -> dict:
        with self.lock:
            self.state = "idle"
            self._cancel_small_timer()
            self._pending.clear()
            self._displayed.clear()
            self._display_index = 0
            self.session.clear()
            self.brain.clear_session()
            log.info("Session cleared")
        return self.status

    def poll(self, since_index: int = 0) -> dict:
        need_small_restart = False

        with self.lock:
            if self.state == "running" and self._pending:
                now = time.time()
                if now - self._last_display_time >= self._next_interval:
                    item = self._pop_next_item()
                    if item:
                        item.index = self._display_index
                        self._display_index += 1
                        self._displayed.append(item)
                        if item.source == "big" and item.commands:
                            self.device_bridge.apply_ai_commands(item.commands)
                        self._last_display_time = now
                        self._next_interval = random.randint(
                            SMALL_MODEL_MIN_INTERVAL, SMALL_MODEL_MAX_INTERVAL
                        )
                        log.debug(
                            "Displayed item %d  source=%s",
                            item.index,
                            item.source,
                        )

            if self.state == "running":
                has_big_pending = any(i.source == "big" for i in self._pending)
                has_big_in_flight = (
                    self._big_thread is not None and self._big_thread.is_alive()
                )
                if (
                    not has_big_pending
                    and not has_big_in_flight
                    and self._small_timer is None
                ):
                    need_small_restart = True

            new_items = self._displayed[since_index:]
            return {
                "ok": True,
                "items": [i.as_dict() for i in new_items],
                "total": len(self._displayed),
                "state": self.state,
                "pending_count": len(self._pending),
            }

        if need_small_restart:
            self._schedule_small()

    @property
    def status(self) -> dict:
        with self.lock:
            return {
                "ok": True,
                "state": self.state,
                "displayed": len(self._displayed),
                "pending": len(self._pending),
                "device_state": self.session.device_state.as_dict(),
                "big_model": self.big_connector.model,
                "small_model": SMALL_MODEL,
                "persona": self._persona,
                "pacing": self._pacing,
            }

    @staticmethod
    def _is_google_model(model: str) -> bool:
        return model in MODEL_OPTIONS

    @staticmethod
    def _is_groq_model(model: str) -> bool:
        return model in GROQ_MODEL_OPTIONS

    def _connector_for_model(self, model: str):
        if self._is_groq_model(model):
            return self.groq_connector
        return self.google_connector

    def _schedule_small(self) -> None:
        with self.lock:
            if self.state != "running":
                return

            # Keep only one scheduling lane for the small model.
            if self._small_in_flight:
                return
            if self._small_timer is not None and self._small_timer.is_alive():
                return

            interval = random.randint(
                SMALL_MODEL_MIN_INTERVAL, SMALL_MODEL_MAX_INTERVAL
            )
            timer = threading.Timer(interval, self._small_tick)
            timer.daemon = True
            self._small_timer = timer

        timer.start()
        log.debug("Small model scheduled in %ds", interval)

    def _small_tick(self) -> None:
        with self.lock:
            # Timer fired; clear timer reference and enforce single-flight.
            self._small_timer = None
            if self.state != "running":
                return
            if self._small_in_flight:
                log.debug("Small tick skipped: generation already in flight")
                return
            self._small_in_flight = True

        if self.state != "running":
            with self.lock:
                self._small_in_flight = False
            return

        try:
            prompt = self.prompt_builder.build_small_prompt(
                session_turns=self.brain.session_turns,
                device_state=self.session.device_state,
                persona=self._persona,
            )

            speech = self.small_connector.generate(
                system_prompt="",
                user_prompt=prompt,
                model=SMALL_MODEL,
            ).strip()

            if not speech:
                log.warning("Small model returned empty speech")
                self._schedule_small()
                return

            with self.lock:
                recent_speeches = [
                    item.speech for item in self._pending[-3:] + self._displayed[-3:]
                ]
                if speech in recent_speeches:
                    log.debug("Duplicate speech rejected: %s", speech[:60])
                    self._schedule_small()
                    return

            turn = Turn(
                index=len(self.brain.session_turns),
                speech=speech,
                commands=Commands(),
            )
            self.brain.record_turns([turn])

            item = DisplayItem(
                source="small",
                speech=speech,
                raw={"speech": speech},
            )

            with self.lock:
                self._pending.append(item)

            log.debug("Small model speech queued (%d chars)", len(speech))

        except Exception as exc:
            log.error("Small model generation failed: %s", exc)

        finally:
            with self.lock:
                self._small_in_flight = False
            self._schedule_small()

    def _request_big_model(self) -> None:
        if self.state != "running":
            log.debug("Skipping big model request — not running")
            return

        self._big_thread = threading.Thread(
            target=self._big_model_worker, daemon=True
        )
        self._big_thread.start()

    def _big_model_worker(self) -> None:
        try:
            prompt_data = self.brain.build_prompt(
                n_turns=self._n_turns,
                selected_persona=self._persona,
                selected_pacing=self._pacing,
            )

            raw_text = self.big_connector.generate(
                system_prompt=prompt_data["system_prompt"],
                user_prompt=prompt_data["user_prompt"],
                model=self.big_connector.model,
            )

            turns = self.parser.parse(raw_text)

            if not turns:
                log.error("Big model returned no parseable turns")
                self._handle_big_failure("Empty parseable response")
                return

            self.brain.record_turns(turns)
            self.session.add_turns(turns)

            with self.lock:
                for turn in turns:
                    self._pending.append(
                        DisplayItem(
                            source="big",
                            speech=turn.speech,
                            commands=turn.commands.as_dict(),
                            raw=turn.raw,
                        )
                    )
                self._cancel_small_timer()

            log.info(
                "Big model returned %d turns  total_pending=%d",
                len(turns),
                len(self._pending),
            )

            if self.state == "running":
                self._request_big_model()
            else:
                log.info("Big model completed while paused — not chaining next request")

        except Exception as exc:
            log.error("Big model generation failed: %s", exc)
            self._handle_big_failure(str(exc))

    def _handle_big_failure(self, reason: str) -> None:
        log.warning("Big model failed (%s). Restarting small model filler.", reason)

        with self.lock:
            has_any_pending = len(self._pending) > 0
            small_timer_alive = (
                self._small_timer is not None and self._small_timer.is_alive()
            )

        if not has_any_pending and not small_timer_alive and self.state == "running":
            self._schedule_small()

        if self.state == "running":
            log.info("Scheduling big model retry in 15s")
            retry_timer = threading.Timer(15.0, self._request_big_model)
            retry_timer.daemon = True
            retry_timer.start()

    def _pop_next_item(self) -> DisplayItem | None:
        if not self._pending:
            return None

        first_big = next(
            (i for i, item in enumerate(self._pending) if item.source == "big"),
            None,
        )
        if first_big is not None and first_big > 0:
            self._pending = self._pending[first_big:]

        return self._pending.pop(0) if self._pending else None

    def _cancel_small_timer(self) -> None:
        if self._small_timer is not None:
            self._small_timer.cancel()
            self._small_timer = None