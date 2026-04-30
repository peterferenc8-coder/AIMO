"""
orchestrator.py
---------------
Producer-consumer session orchestrator.

  - Producer (generator loop): Monitors buffer depth. When it drops below
    the low watermark, fires the big model to fill back to high watermark.
    Sleeps otherwise. No small model.

  - Consumer (display loop): Pops one item from the buffer every N seconds,
    applies device commands, synthesises speech via Kokoro TTS, and appends
    to the displayed stream.  The frontend receives word-level timestamps
    so it can highlight each word exactly when it is spoken.

  - Frontend poll(): Read-only. Returns newly displayed items since last call.
"""

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from device_bridge import get_bridge

from config import (
    BIG_MODEL_RETRY_DELAY,
    DEFAULT_TURNS,
    DISPLAY_INTERVAL,
    GENERATOR_SLEEP,
    GROQ_MODEL_OPTIONS,
    HIGH_WATERMARK,
    LOW_WATERMARK,
    MODEL_OPTIONS,
    SMALL_MODEL,
)
from ai_connector import GoogleAIConnector, GroqAIConnector
from brain import Brain
from prompt_builder import PromptBuilder
from response_parser import Commands, ResponseParser, Turn
from session_manager import SessionManager
from settings_store import load_settings
import tts

log = logging.getLogger(__name__)


@dataclass
class DisplayItem:
    source: str  # "big"
    speech: str
    commands: dict = field(default_factory=dict)
    raw: Any = None
    index: int = 0
    # ── TTS fields ────────────────────────────────────────────────────────
    audio_url: str | None = None
    words: list[dict] = field(default_factory=list)   # [{word, start_ms, end_ms}]
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "speech": self.speech,
            "commands": self.commands,
            "raw": self.raw,
            "index": self.index,
            "audio_url": self.audio_url,
            "words": self.words,
            "duration_ms": self.duration_ms,
        }


class SessionOrchestrator:
    """
    Producer-consumer orchestrator with watermark-based backpressure.
    """

    # ── Watermark & timing settings (from config, overridable via env) ─────
    DISPLAY_INTERVAL = DISPLAY_INTERVAL
    LOW_WATERMARK = LOW_WATERMARK
    HIGH_WATERMARK = HIGH_WATERMARK
    GENERATOR_SLEEP = GENERATOR_SLEEP
    RETRY_DELAY = BIG_MODEL_RETRY_DELAY

    def __init__(self):
        self._settings = load_settings()
        self.tts_enabled = self._settings.get("tts_enabled", True)
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

        self.lock = threading.RLock()

        self.state = "idle"

        # Buffer: pending items waiting to be displayed
        self._pending: list[DisplayItem] = []
        # Already-displayed items (for frontend poll)
        self._displayed: list[DisplayItem] = []
        self._display_index = 0
        self._consecutive_failures = 0

        # Threads
        self._display_thread: threading.Thread | None = None
        self._generator_thread: threading.Thread | None = None
        self._big_thread: threading.Thread | None = None
        self._big_in_flight = False

        # Session params
        self._n_turns = DEFAULT_TURNS
        self._persona: str | None = None
        self._pacing: str | None = None

        self.device_bridge = get_bridge()

    # ── Settings & lifecycle ────────────────────────────────────────────────

    def apply_settings(self, settings: dict[str, str]) -> dict[str, str]:
        """Update live connectors and prompt assets from saved settings."""
        self._settings.update(settings)
        self.tts_enabled = self._settings.get("tts_enabled", True)
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
            self._n_turns = self.HIGH_WATERMARK
            self._persona = resolved_persona
            self._pacing = resolved_pacing
            self._pending.clear()
            self._displayed.clear()
            self._display_index = 0

            self.session.clear()
            self.brain.clear_session()

            if model:
                self.big_connector = self._connector_for_model(model)
                self.big_connector.model = model

        # ── START SESSION: send system prompt once ─────────────────────────
        try:
            system_prompt = self.brain.get_system_prompt()
            self.big_connector.start_session(system_prompt)
            log.info("Started chat session with %s", self.big_connector.model)

            # Send the seed prompt (persona, pacing, opening pattern)
            seed_prompt = self.brain.build_seed_prompt(
                selected_persona=resolved_persona,
                selected_pacing=resolved_pacing,
            )
            raw_text = self.big_connector.send_message(seed_prompt)
            turns = self.parser.parse(raw_text)

            if turns:
                self.brain.record_turns(turns)
                self.session.add_turns(turns)

                with self.lock:
                    for turn in turns:
                        self._pending.append(
                            self._build_display_item(turn)
                        )
                log.info("Seed prompt returned %d turns", len(turns))
            else:
                log.warning("Seed prompt returned no parseable turns")

        except Exception as exc:
            log.error("Failed to start session: %s", exc)
            with self.lock:
                self.state = "idle"
            return {
                "ok": False,
                "error": f"Failed to start session: {exc}",
                "state": "idle",
            }

        log.info(
            "Session started  turns=%d  persona=%s  pacing=%s  big_model=%s",
            self.HIGH_WATERMARK,
            resolved_persona,
            resolved_pacing,
            self.big_connector.model,
        )

        # Start the two independent loops
        self._display_thread = threading.Thread(
            target=self._display_loop, daemon=True, name="display"
        )
        self._generator_thread = threading.Thread(
            target=self._generator_loop, daemon=True, name="generator"
        )
        self._display_thread.start()
        self._generator_thread.start()

        return self.status

    def pause(self) -> dict:
        with self.lock:
            if self.state == "running":
                self.state = "paused"
                log.info("Session paused")
        return self.status

    def resume(self) -> dict:
        with self.lock:
            if self.state == "paused":
                self.state = "running"
                log.info("Session resumed")
        return self.status

    def clear(self) -> dict:
        with self.lock:
            self.state = "idle"
            self._pending.clear()
            self._displayed.clear()
            self._display_index = 0
            self.session.clear()
            self.brain.clear_session()
            self._big_in_flight = False

            # End the chat session
            try:
                self.big_connector.end_session()
            except Exception as exc:
                log.warning("Error ending session: %s", exc)

            log.info("Session cleared")
        return self.status

    # ── Poll: read-only, returns newly displayed items ──────────────────────

    def poll(self, since_index: int = 0) -> dict:
        with self.lock:
            new_items = self._displayed[since_index:]
            return {
                "ok": True,
                "items": [i.as_dict() for i in new_items],
                "total": len(self._displayed),
                "state": self.state,
                "pending_count": len(self._pending),
            }

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

    # ── Display loop (consumer) ─────────────────────────────────────────────

    def _display_loop(self) -> None:
        """
        Steady clock: pop one item from the buffer every DISPLAY_INTERVAL seconds.
        Applies device commands and records the display.

        TTS synthesis happens here so that audio generation time does NOT
        block the producer (model generation) or the poll() endpoint.
        """
        while True:
            should_sleep = False
            item: DisplayItem | None = None

            with self.lock:
                if self.state == "idle":
                    break
                if self.state == "paused":
                    should_sleep = True
                elif self._pending:
                    item = self._pending.pop(0)
                    item.index = self._display_index
                    self._display_index += 1
                    self._displayed.append(item)

                    if item.commands:
                        self.device_bridge.apply_ai_commands(item.commands)

                    log.debug(
                        "Displayed item %d  pending=%d",
                        item.index,
                        len(self._pending),
                    )

            if should_sleep:
                time.sleep(0.5)
            else:
                time.sleep(self.DISPLAY_INTERVAL)

    # ── Generator loop (producer) ─────────────────────────────────────────

    def _generator_loop(self) -> None:
        max_backoff = 60.0

        while True:
            should_sleep_paused = False
            should_generate = False
            buffer_depth = 0

            with self.lock:
                if self.state == "idle":
                    break
                if self.state == "paused":
                    should_sleep_paused = True
                else:
                    buffer_depth = len(self._pending)
                    should_generate = (
                        buffer_depth <= self.LOW_WATERMARK
                        and not self._big_in_flight
                    )

            if should_sleep_paused:
                time.sleep(1.0)
            elif should_generate:
                log.info("Buffer low (%d <= %d) — requesting big model", buffer_depth, self.LOW_WATERMARK)
                self._request_big_model()

                # Adaptive wait: longer after each failure
                backoff = min(5.0 * (2 ** self._consecutive_failures), max_backoff)
                time.sleep(backoff)
            else:
                with self.lock:
                    self._consecutive_failures = 0
                time.sleep(self.GENERATOR_SLEEP)

    # ── Big model worker ────────────────────────────────────────────────────

    def _request_big_model(self) -> None:
        with self.lock:
            if self.state != "running":
                log.debug("Skipping big model request — not running")
                return
            if self._big_in_flight:
                log.debug("Big model already in flight — skipping duplicate request")
                return
            self._big_in_flight = True

        self._big_thread = threading.Thread(
            target=self._big_model_worker, daemon=True, name="big-model"
        )
        self._big_thread.start()

    def _big_model_worker(self) -> None:
        """
        Generates one batch of HIGH_WATERMARK turns and appends to the buffer.
        Uses stateful chat: only sends minimal user prompt, not full system prompt.

        TTS is pre-generated for each turn so the display loop can serve
        audio immediately without waiting.
        """
        try:
            # Build minimal user prompt with fresh context only
            user_prompt = self.brain.build_turn_prompt(
                n_turns=self.HIGH_WATERMARK,
                device_state=self.session.device_state,
            )

            # Send message in existing session (system prompt already set)
            raw_text = self.big_connector.send_message(user_prompt)

            turns = self.parser.parse(raw_text)

            if not turns:
                log.error("Big model returned no parseable turns")
                self._handle_big_failure("Empty parseable response")
                return

            self.brain.record_turns(turns)
            self.session.add_turns(turns)

            # Pre-generate TTS for each turn (parallelise if desired)
            display_items: list[DisplayItem] = []
            for turn in turns:
                item = self._build_display_item(turn)
                display_items.append(item)

            with self.lock:
                for item in display_items:
                    self._pending.append(item)

            log.info(
                "Big model returned %d turns  total_pending=%d",
                len(turns),
                len(self._pending),
            )

        except Exception as exc:
            log.error("Big model generation failed: %s", exc)
            self._handle_big_failure(str(exc))

        finally:
            with self.lock:
                self._big_in_flight = False

    def _build_display_item(self, turn: Turn) -> DisplayItem:
        """
        Build a DisplayItem from a parsed Turn, running TTS synthesis
        to obtain audio and word-level timestamps (if TTS is enabled).
        """
        speech = turn.speech or ""
        tts_meta: dict = {}

        if speech.strip() and self.tts_enabled:
            try:
                tts_meta = tts.synthesize(speech)
            except Exception as exc:
                log.warning("TTS synthesis failed for turn %d: %s", turn.index, exc)
                tts_meta = {
                    "audio_url": None,
                    "audio_path": None,
                    "words": [],
                    "duration_ms": 0,
                }
        elif speech.strip() and not self.tts_enabled:
            # TTS is disabled, provide empty audio metadata
            tts_meta = {
                "audio_url": None,
                "audio_path": None,
                "words": [],
                "duration_ms": 0,
            }

        return DisplayItem(
            source="big",
            speech=speech,
            commands=turn.commands.as_dict(),
            raw=turn.raw,
            audio_url=tts_meta.get("audio_url"),
            words=tts_meta.get("words", []),
            duration_ms=tts_meta.get("duration_ms", 0),
        )

    def _handle_big_failure(self, reason: str) -> None:
        with self.lock:
            self._consecutive_failures += 1
        log.warning("Big model failed (%s). Failure #%d. Backoff increasing.", reason, self._consecutive_failures)

    # ── Helpers ─────────────────────────────────────────────────────────────

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
