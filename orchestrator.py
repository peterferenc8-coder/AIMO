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
from intent_compiler import IntentCompiler

from device_bridge import get_bridge

from config import (
    BIG_MODEL_RETRY_DELAY,
    DEFAULT_TURNS,
    DISPLAY_INTERVAL,
    GENERATOR_SLEEP,
    GOOGLE_TIMEOUT,
    GROQ_MODEL_OPTIONS,
    GROQ_TIMEOUT,
    HIGH_WATERMARK,
    LOW_WATERMARK,
    MODEL_OPTIONS,
    VIDEO_CHANCE,
)
from ai_connector import GoogleAIConnector, GroqAIConnector
from brain import Brain
from feedback_store import add_banned_phrase, remove_banned_phrase
from response_parser import Commands, ResponseParser, Turn
from session_manager import SessionManager
from settings_store import load_settings
from stash_client import StashClient
import tts

log = logging.getLogger(__name__)

# Spoken before a clip when the model supplied no line of its own. The clip
# takes over the screen once this finishes, so it doubles as the hand-off.
VIDEO_INTRO_LINES = [
    "I am going to play a video for you now. Enjoy!",
    "Let me put something on for you. Enjoy it.",
    "I found a clip for you. Watch this.",
    "Here, watch this one with me.",
]


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
    # Mouth-shape track for the avatar's lip-sync, aligned to the same audio
    # clock as *words*: [{t_ms, dur_ms, viseme, weight}]
    visemes: list[dict] = field(default_factory=list)
    duration_ms: int = 0
    # ── Video clip (play_video intent) ──────────────────────────────────────
    # When set, the frontend plays this Stash clip instead of running device
    # motion; the clip's funscript (if any) drives the device.
    video: dict | None = None   # {scene_id, video_url, has_funscript, duration_ms, title}

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "speech": self.speech,
            "commands": self.commands,
            "raw": self.raw,
            "index": self.index,
            "audio_url": self.audio_url,
            "words": self.words,
            "visemes": self.visemes,
            "duration_ms": self.duration_ms,
            "video": self.video,
        }


class SessionOrchestrator:
    """
    Producer-consumer orchestrator with watermark-based backpressure.
    """

    # ── Watermark & timing settings (defaults; overridden from settings) ────
    DISPLAY_INTERVAL = DISPLAY_INTERVAL
    LOW_WATERMARK = LOW_WATERMARK
    HIGH_WATERMARK = HIGH_WATERMARK
    GENERATOR_SLEEP = GENERATOR_SLEEP
    RETRY_DELAY = BIG_MODEL_RETRY_DELAY
    BIG_MAX_RETRIES = 3

    def __init__(self):
        self._settings = load_settings()
        self._apply_tunables()
        self.tts_enabled = self._settings.get("tts_enabled", True)
        self.video_enabled = self._settings.get("stash_video_enabled", True)
        self.google_connector = GoogleAIConnector(
            api_key=self._settings.get("google_api_key", ""),
            model=self._settings.get("google_model", "gemma-4-31b-it"),
            timeout=self._settings.get("google_timeout", GOOGLE_TIMEOUT),
            gen_options=self._gen_options(),
        )
        self.groq_connector = GroqAIConnector(
            api_key=self._settings.get("groq_api_key", ""),
            model=self._settings.get("groq_model", "openai/gpt-oss-120b"),
            timeout=self._settings.get("groq_timeout", GROQ_TIMEOUT),
            gen_options=self._gen_options(),
        )

        default_model = self._settings.get("google_model", self.google_connector.model)
        self.big_connector = self._connector_for_model(default_model)
        self.big_connector.model = default_model

        self.brain = Brain()
        self.parser = ResponseParser()
        self.session = SessionManager()

        self.lock = threading.RLock()
        self.intent_compiler = IntentCompiler()
        self.stash = StashClient(
            url=self._settings.get("stash_url", ""),
            api_key=self._settings.get("stash_api_key", ""),
            tag=self._settings.get("stash_tag", ""),
            proxy_enabled=self._settings.get("stash_proxy_enabled", False),
            proxy_address=self._settings.get("stash_proxy_address", ""),
        )
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
        # Bumped on every start(); the display/generator loops capture their
        # epoch and exit as soon as it changes. This kills threads from a prior
        # session that are still mid-sleep when a new session begins (otherwise
        # they resurrect — they'd wake to find state back at "running" and keep
        # draining the buffer, doubling the effective display rate).
        self._session_epoch = 0

        # Set by the frontend when an on-screen video clip stops playing
        # (natural end, seek-to-end, skip, or close). Releases the display
        # hold so AI device motion resumes immediately.
        self._video_done = threading.Event()

        # Session params
        self._n_turns = self._settings.get("default_turns", DEFAULT_TURNS)
        self._persona: str | None = None
        self._pacing: str | None = None

        self.device_bridge = get_bridge()

        self._push_collaborator_settings()

    # ── Settings & lifecycle ────────────────────────────────────────────────

    def _gen_options(self) -> dict:
        """Generation hyperparameters from current settings."""
        return {
            "temperature": float(self._settings.get("gen_temperature", 1.2)),
            "top_p": float(self._settings.get("gen_top_p", 0.90)),
            "top_k": int(self._settings.get("gen_top_k", 60)),
        }

    def _apply_tunables(self) -> None:
        """Refresh scalar tunable instance attributes from the settings dict."""
        s = self._settings
        self.DISPLAY_INTERVAL = float(s.get("display_interval", DISPLAY_INTERVAL))
        self.LOW_WATERMARK = int(s.get("low_watermark", LOW_WATERMARK))
        self.HIGH_WATERMARK = int(s.get("high_watermark", HIGH_WATERMARK))
        self.GENERATOR_SLEEP = float(s.get("generator_sleep", GENERATOR_SLEEP))
        self.RETRY_DELAY = int(s.get("big_model_retry_delay", BIG_MODEL_RETRY_DELAY))
        self.BIG_MAX_RETRIES = int(s.get("big_model_max_retries", self.BIG_MAX_RETRIES))
        self.video_chance = float(s.get("video_chance", VIDEO_CHANCE))

    def _push_collaborator_settings(self) -> None:
        """Push tunables into collaborators that own their own config."""
        s = self._settings
        self.brain.prompt_builder.banned_phrase_window = int(
            s.get("banned_phrase_window", 20)
        )
        tts.configure(
            voice=s.get("kokoro_voice"),
            speed=s.get("kokoro_speed"),
            device=s.get("kokoro_device"),
        )
        try:
            import rvc_client as rvc
            rvc.configure(
                enabled=s.get("rvc_enabled"),
                pitch=s.get("rvc_pitch"),
                index_rate=s.get("rvc_index_rate"),
            )
        except Exception:  # noqa: BLE001 - RVC is optional
            pass

    def apply_settings(self, settings: dict[str, str]) -> dict[str, str]:
        """Update live connectors and prompt assets from saved settings."""
        self._settings.update(settings)
        self._apply_tunables()
        self._push_collaborator_settings()
        self.tts_enabled = self._settings.get("tts_enabled", True)
        self.video_enabled = self._settings.get("stash_video_enabled", True)
        gen = self._gen_options()
        self.google_connector.reconfigure(
            api_key=self._settings.get("google_api_key", ""),
            model=self._settings.get("google_model", self.google_connector.model),
            timeout=self._settings.get("google_timeout", GOOGLE_TIMEOUT),
            gen_options=gen,
        )
        self.groq_connector.reconfigure(
            api_key=self._settings.get("groq_api_key", ""),
            model=self._settings.get("groq_model", self.groq_connector.model),
            timeout=self._settings.get("groq_timeout", GROQ_TIMEOUT),
            gen_options=gen,
        )
        active_model = self.big_connector.model
        self.big_connector = self._connector_for_model(active_model)

        self.stash.configure(
            url=self._settings.get("stash_url", ""),
            api_key=self._settings.get("stash_api_key", ""),
            tag=self._settings.get("stash_tag", ""),
            proxy_enabled=self._settings.get("stash_proxy_enabled", False),
            proxy_address=self._settings.get("stash_proxy_address", ""),
        )

        self.brain.prompt_builder.reload()
        return self._settings

    def reload_prompts(self) -> None:
        self.brain.prompt_builder.reload()

    def _compile_turns(self, turns: list[Turn], label: str = "Intent") -> list[Turn]:
        """
        Resolve each turn's narrative intent into concrete device commands.

        Turns are mutated in place and returned. A turn whose intent the
        compiler rejects falls through with whatever commands the LLM supplied
        (legacy behaviour), so one bad intent never drops a turn.
        """
        compiled_turns: list[Turn] = []
        for turn in turns:
            self._maybe_inject_video(turn)
            if self._is_video_intent(turn):
                # play_video is resolved to a Stash clip in _build_display_item;
                # it carries no device command of its own.
                pass
            elif turn.intent and turn.ai_intensity is not None:
                try:
                    compiled = self.intent_compiler.compile(
                        intent=turn.intent,
                        intensity=turn.ai_intensity,
                    )
                    # Pattern-level intensity goes into the legacy
                    # commands.intensity field.
                    turn.commands = Commands(
                        pattern=compiled.pattern,
                        speed=compiled.speed,
                        depth=compiled.depth,
                        base=compiled.base,
                        intensity=compiled.pattern_intensity,
                    )
                    if turn.duration_ms is None:
                        turn.duration_ms = compiled.duration_ms
                except ValueError as exc:
                    log.warning(
                        "%s compilation failed for %s@%s: %s",
                        label, turn.intent, turn.ai_intensity, exc
                    )
            compiled_turns.append(turn)
        return compiled_turns

    def _enqueue_turns(self, compiled_turns: list[Turn]) -> None:
        """
        Record compiled turns into session history and queue them for display.

        Display items are built before the lock is taken: _build_display_item
        can hit Stash to resolve a clip, which must not block the display loop.
        """
        self.brain.record_turns(compiled_turns)
        self.session.add_turns(compiled_turns)

        display_items = [self._build_display_item(t) for t in compiled_turns]
        with self.lock:
            self._pending.extend(display_items)

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
            self._session_epoch += 1
            session_epoch = self._session_epoch
            self._n_turns = self.HIGH_WATERMARK
            self._persona = resolved_persona
            self._pacing = resolved_pacing
            self._pending.clear()
            self._displayed.clear()
            self._display_index = 0
            self._big_in_flight = False

            self.session.clear()
            self.brain.clear_session()

            if model:
                self.big_connector = self._connector_for_model(model)
                self.big_connector.model = model

        # ── START SESSION: send system prompt once ─────────────────────────
        try:
            system_prompt = self.brain.get_system_prompt(video_enabled=self.video_enabled)
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
                compiled_turns = self._compile_turns(turns, label="Seed intent")
                self._enqueue_turns(compiled_turns)
                log.info("Seed prompt returned %d turns", len(compiled_turns))
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

        # Start the two independent loops, bound to this session's epoch.
        self._display_thread = threading.Thread(
            target=self._display_loop, args=(session_epoch,), daemon=True, name="display"
        )
        self._generator_thread = threading.Thread(
            target=self._generator_loop, args=(session_epoch,), daemon=True, name="generator"
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

    def notify_video_ended(self) -> dict:
        """
        Signal that the on-screen video clip has stopped playing.

        The display loop holds the AI while a clip plays; this releases that
        hold immediately so the next turn's device motion resumes — instead of
        waiting out the clip's full length (which left the device idle when the
        user seeked to the end or skipped).
        """
        self._video_done.set()
        return {"ok": True, "state": self.state}

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

    # ── Feedback: user reactions to displayed turns ─────────────────────────

    #: Reactions that are transient (attached to the in-memory Turn and only
    #: matter while it stays inside the rolling banned-phrase window).
    _ROLLING_REACTIONS = {"like", "love", "dislike"}

    def record_feedback(self, index: Any, reaction: Any) -> dict:
        """
        Record (or clear) a user reaction to the displayed turn at ``index``.

        Accepted actions:
          - ``like`` / ``love`` / ``dislike`` — set the transient reaction on
            the in-memory Turn. It influences prompts only while that turn
            remains within ``BANNED_PHRASE_WINDOW``.
          - ``clear`` — remove that transient reaction.
          - ``ban`` — persist the spoken line to the cross-session banned store
            so it is never said again. Independent of the transient reaction.
          - ``unban`` — remove the spoken line from the banned store.

        Display index N maps to ``brain.session_turns[N]`` because turns are
        recorded and displayed in the same FIFO order.
        """
        reaction = str(reaction).strip().lower() if reaction is not None else ""
        valid = self._ROLLING_REACTIONS | {"clear", "ban", "unban"}
        if reaction not in valid:
            return {"ok": False, "error": f"Unknown reaction: {reaction!r}"}

        try:
            index = int(index)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Invalid index"}

        with self.lock:
            turns = self.brain.session_turns
            if index < 0 or index >= len(turns):
                return {"ok": False, "error": f"No turn at index {index}"}
            turn = turns[index]
            speech = turn.speech
            if reaction in self._ROLLING_REACTIONS:
                turn.reaction = reaction
            elif reaction == "clear":
                turn.reaction = None

        # Bans live in the persistent store (file IO) outside the lock and are
        # independent of the transient reaction above.
        result = {"ok": True, "index": index, "reaction": reaction}
        if reaction == "ban" and speech:
            result["banned"] = add_banned_phrase(speech)
        elif reaction == "unban" and speech:
            result["unbanned"] = remove_banned_phrase(speech)

        log.info("Feedback recorded: index=%d reaction=%s", index, reaction)
        return result

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
                "persona": self._persona,
                "pacing": self._pacing,
            }

    # ── Display loop (consumer) ─────────────────────────────────────────────

    def _display_loop(self, epoch: int) -> None:
        """
        Steady clock: pop one item from the buffer every DISPLAY_INTERVAL seconds.
        Applies device commands and records the display.

        TTS synthesis happens here so that audio generation time does NOT
        block the producer (model generation) or the poll() endpoint.

        Bound to ``epoch``: exits the moment a newer session starts, so a thread
        left mid-sleep by a stop/restart can't keep draining the buffer.
        """
        while True:
            should_sleep = False
            item: DisplayItem | None = None

            with self.lock:
                if self.state == "idle" or epoch != self._session_epoch:
                    break
                if self.state == "paused":
                    should_sleep = True
                elif self._pending:
                    item = self._pending.pop(0)
                    item.index = self._display_index
                    self._display_index += 1
                    self._displayed.append(item)

                    if item.video:
                        # The clip's funscript drives the device (started by the
                        # frontend); stop the current AI motion so it doesn't fight.
                        # Arm the done-signal so we can release the hold the moment
                        # the clip actually stops playing.
                        self._video_done.clear()
                        self.device_bridge.apply_ai_commands({"pattern": "stop"})
                    elif item.commands:
                        get_bridge().apply_ai_commands(item.commands)

                    log.debug(
                        "Displayed item %d  pending=%d",
                        item.index,
                        len(self._pending),
                    )

            if should_sleep:
                time.sleep(0.5)
            elif item is not None and item.video:
                # Hold the AI loop while the clip plays so the next turn's device
                # command doesn't interrupt playback mid-clip. Release as soon as
                # the frontend reports the clip stopped (end, seek-to-end, skip),
                # falling back to the clip's real length if no signal arrives.
                # The clip only starts once the announcement has finished
                # speaking, so the fallback has to cover the line as well or it
                # expires mid-clip when no "ended" signal arrives.
                hold_ms = item.video.get("duration_ms") or 0
                if hold_ms:
                    hold_ms += item.duration_ms or 0
                self._wait_video_done(hold_ms / 1000.0 if hold_ms else self.DISPLAY_INTERVAL)
            else:
                self._sleep_session(self.DISPLAY_INTERVAL, epoch)

    def _sleep_while_running(self, seconds: float) -> None:
        """Sleep up to `seconds`, waking early if the session is stopped/cleared."""
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            with self.lock:
                if self.state == "idle":
                    return
            time.sleep(min(0.5, deadline - time.monotonic()))

    def _sleep_session(self, seconds: float, epoch: int) -> None:
        """
        Sleep up to `seconds`, waking early if the session is stopped or a newer
        session has started (epoch changed). Keeps long display/generation waits
        responsive to stop/restart instead of running out the full interval.
        """
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            with self.lock:
                if self.state == "idle" or epoch != self._session_epoch:
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.5, remaining))

    def _wait_video_done(self, max_seconds: float) -> None:
        """
        Wait until the clip stops playing, the session is stopped, or
        `max_seconds` elapses as a safety fallback — whichever comes first.
        """
        deadline = time.monotonic() + max(0.0, max_seconds)
        while True:
            with self.lock:
                if self.state == "idle":
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self._video_done.wait(timeout=min(0.5, remaining)):
                return

    # ── Generator loop (producer) ─────────────────────────────────────────

    def _generator_loop(self, epoch: int) -> None:
        max_backoff = 60.0

        while True:
            should_sleep_paused = False
            should_generate = False
            buffer_depth = 0

            with self.lock:
                if self.state == "idle" or epoch != self._session_epoch:
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
                self._sleep_session(1.0, epoch)
            elif should_generate:
                log.info("Buffer low (%d <= %d) — requesting big model", buffer_depth, self.LOW_WATERMARK)
                self._request_big_model()

                # Adaptive wait: longer after each failure
                backoff = min(5.0 * (2 ** self._consecutive_failures), max_backoff)
                self._sleep_session(backoff, epoch)
            else:
                with self.lock:
                    self._consecutive_failures = 0
                self._sleep_session(self.GENERATOR_SLEEP, epoch)

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
        try:
            user_prompt = self.brain.build_turn_prompt(
                n_turns=self.HIGH_WATERMARK,
                device_state=self.session.device_state,
            )

            raw_text = self._send_big_with_retries(user_prompt)
            turns = self.parser.parse(raw_text)

            if not turns:
                log.error("Big model returned no parseable turns")
                self._handle_big_failure("Empty parseable response")
                return

            compiled_turns = self._compile_turns(turns, label="Intent")
            self._enqueue_turns(compiled_turns)

            log.info(
                "Big model returned %d turns  total_pending=%d",
                len(compiled_turns),
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
        # Resolve the clip first: whether this is a video turn decides whether
        # the turn needs an announcement line below.
        video = self._resolve_video(turn) if self._is_video_intent(turn) else None

        speech = turn.speech or ""
        # A video turn speaks *before* the clip rather than over it — the clip
        # replaces the text and avatar while it plays, and its own audio would
        # fight a voice-over. If the model gave us no line for the turn, use a
        # stock one so the cut to video is still introduced.
        if video and not speech.strip():
            speech = random.choice(VIDEO_INTRO_LINES)

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

        # A video turn drives the device via its funscript, not via AI motion,
        # so it carries no device command block.
        commands = {} if video else turn.commands.as_dict()

        return DisplayItem(
            source="big",
            speech=speech,
            commands=commands,
            raw=turn.raw,
            audio_url=tts_meta.get("audio_url"),
            words=tts_meta.get("words", []),
            visemes=tts_meta.get("visemes", []),
            duration_ms=tts_meta.get("duration_ms", 0),
            video=video,
        )

    @staticmethod
    def _is_video_intent(turn: Turn) -> bool:
        return turn.intent == "play_video"

    def _maybe_inject_video(self, turn: Turn) -> None:
        """
        Randomly promote a normal turn into a play_video interlude.

        The LLM almost never selects play_video on its own, so we force it at
        VIDEO_CHANCE per turn. Only do this when Stash is configured (otherwise
        the turn would resolve to no clip and the device would idle).
        """
        if not self.video_enabled:
            return
        if self.video_chance <= 0 or self._is_video_intent(turn):
            return
        if not self.stash.is_configured():
            return
        if random.random() < self.video_chance:
            log.info("Injecting play_video interlude (chance=%.2f)", self.video_chance)
            turn.intent = "play_video"

    def _resolve_video(self, turn: Turn) -> dict | None:
        """Pick a random tagged Stash scene for a play_video turn."""
        if not self.video_enabled:
            log.info("play_video intent but video playback is disabled — ignoring")
            return None
        if not self.stash.is_configured():
            log.warning("play_video intent but Stash is not configured — ignoring")
            return None
        try:
            scene = self.stash.pick_random_scene(prefer_interactive=True)
        except Exception as exc:  # noqa: BLE001 - never let Stash break the loop
            log.warning("Stash scene lookup failed: %s", exc)
            return None
        if not scene:
            log.warning("play_video intent but no tagged scenes available")
            return None
        log.info("play_video -> scene %s (%s)", scene["id"], scene["title"])
        return {
            "scene_id": scene["id"],
            "title": scene["title"],
            "video_url": f"/api/stash/video/{scene['id']}",
            "has_funscript": scene["has_funscript"],
            "duration_ms": scene["duration_ms"],
        }

    def _send_big_with_retries(self, user_prompt: str) -> str:
        """
        Send a message to the big model, retrying transient failures up to
        BIG_MAX_RETRIES times with RETRY_DELAY seconds between attempts.
        """
        attempts = max(1, self.BIG_MAX_RETRIES)
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self.big_connector.send_message(user_prompt)
            except Exception as exc:  # noqa: BLE001 - surfaced after retries exhausted
                last_exc = exc
                if attempt >= attempts:
                    break
                log.warning(
                    "Big model attempt %d/%d failed (%s) — retrying in %ds",
                    attempt, attempts, exc, self.RETRY_DELAY,
                )
                # Sleep in short slices so a stop request isn't blocked.
                deadline = time.monotonic() + self.RETRY_DELAY
                while time.monotonic() < deadline:
                    with self.lock:
                        if self.state == "idle":
                            raise RuntimeError("Session stopped during retry") from last_exc
                    time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        raise last_exc if last_exc else RuntimeError("Big model send failed")

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
