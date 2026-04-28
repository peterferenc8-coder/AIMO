"""
ai_connector.py
---------------
Thin wrappers around Google and Groq Generative AI APIs.

Now stateful: system prompt is set once per session, then only
new user messages are sent each turn.
"""

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import google.genai as genai

from config import (
    GENERATION_OPTIONS,
    GOOGLE_MODEL,
    GOOGLE_TIMEOUT,
    GROQ_MODEL,
    GROQ_TIMEOUT,
)

log = logging.getLogger(__name__)


# ── Base connector ───────────────────────────────────────────────────────────

class BaseAIConnector:
    """
    Shared machinery for API health tracking, validation, and response logging.
    """

    def __init__(self, *, api_key: str, model: str, timeout: int, log_dir_name: str):
        self.api_key = ""
        self.model = model
        self.timeout = timeout
        self.response_log_dir = (
            Path(__file__).resolve().parent / "logs" / log_dir_name
        )

        self._last_api_ok: bool | None = None
        self._last_api_message: str = "Not validated yet"
        self._last_api_checked_at: str | None = None

        # Session state
        self._session_active: bool = False
        self._system_prompt: str = ""

        self.reconfigure(api_key=api_key, model=model, timeout=timeout)

    def reconfigure(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        """Update connection settings in place without replacing callers."""
        if api_key is not None:
            self.api_key = api_key.strip()
        if model is not None:
            self.model = model
        if timeout is not None:
            self.timeout = timeout

    def health_check(self) -> dict[str, Any]:
        configured = self._is_configured()
        return {
            "ok": False if not configured else (True if self._last_api_ok is None else self._last_api_ok),
            "message": "API key not configured" if not configured else self._last_api_message,
            "model": self.model,
            "checked_at": self._last_api_checked_at,
            "session_active": self._session_active,
        }

    def validate_api_key(self) -> dict[str, Any]:
        if not self._is_configured():
            self._mark_unhealthy(ValueError("API key not configured"))
            return self.health_check()

        try:
            self._do_validation_call()
            self._mark_healthy()
        except Exception as exc:
            self._mark_unhealthy(exc)

        return self.health_check()

    # ── Session management ────────────────────────────────────────────────────

    def start_session(self, system_prompt: str) -> None:
        """
        Start a new chat session with the given system prompt.
        This is called once at the beginning of a session.
        """
        self._system_prompt = system_prompt
        self._session_active = True
        self._start_chat_session(system_prompt)
        log.info("Started new chat session (%d chars system prompt)", len(system_prompt))

    def end_session(self) -> None:
        """End the current session and clear state."""
        self._session_active = False
        self._system_prompt = ""
        self._end_chat_session()
        log.info("Ended chat session")

    def send_message(self, user_prompt: str, model: str | None = None) -> str:
        """
        Send a user message in the current session.
        Must call start_session() first.
        """
        if not self._session_active:
            raise RuntimeError("No active session. Call start_session() first.")

        selected_model = model or self.model
        self.model = selected_model

        log.debug(
            "Sending message in session  model=%s  user_chars=%d",
            selected_model,
            len(user_prompt),
        )

        try:
            response = self._send_chat_message(user_prompt, selected_model)
            self._write_response_log(
                {
                    "system_prompt": self._system_prompt,
                    "user_prompt": user_prompt,
                    "response": response,
                }
            )
            self._mark_healthy()
            return response

        except Exception as exc:
            self._mark_unhealthy(exc)
            raise RuntimeError(f"{self.__class__.__name__} error: {str(exc)[:300]}") from exc

    # ── Abstract methods ──────────────────────────────────────────────────────

    def _start_chat_session(self, system_prompt: str) -> None:
        raise NotImplementedError

    def _end_chat_session(self) -> None:
        raise NotImplementedError

    def _send_chat_message(self, user_prompt: str, model: str) -> str:
        raise NotImplementedError

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _mark_healthy(self) -> None:
        self._last_api_ok = True
        self._last_api_message = "Connected"
        self._last_api_checked_at = datetime.now(timezone.utc).isoformat()

    def _mark_unhealthy(self, exc: Exception) -> None:
        self._last_api_ok = False
        self._last_api_message = str(exc)[:100]
        self._last_api_checked_at = datetime.now(timezone.utc).isoformat()

    def _write_response_log(self, log_data: dict[str, Any]) -> None:
        try:
            self.response_log_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%fZ")
            log_file = self.response_log_dir / f"response_{timestamp}.json"

            with log_file.open("w", encoding="utf-8") as handle:
                json.dump(log_data, handle, indent=2, ensure_ascii=False)
                handle.write("\n")

            log.debug("Wrote API response log to %s", log_file)

        except OSError as exc:
            log.warning("Failed to write API response log: %s", exc)

    def _is_configured(self) -> bool:
        raise NotImplementedError

    def _do_validation_call(self) -> None:
        raise NotImplementedError


# ── Google ───────────────────────────────────────────────────────────────────

class GoogleAIConnector(BaseAIConnector):
    """
    Talks to Google's Generative AI API (Gemini/Gemma models) using
    stateful chat sessions.
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = GOOGLE_MODEL,
        timeout: int = GOOGLE_TIMEOUT,
    ):
        self.client = None
        self._chat_session = None
        super().__init__(
            api_key=api_key,
            model=model,
            timeout=timeout,
            log_dir_name="google_api_responses",
        )

    def reconfigure(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        super().reconfigure(api_key=api_key, model=model, timeout=timeout)
        if api_key is not None:
            self.client = genai.Client(api_key=self.api_key) if self.api_key else None

    def _is_configured(self) -> bool:
        return self.client is not None

    def _do_validation_call(self) -> None:
        # Quick validation: generate a single token
        self.client.models.generate_content(
            model=f"models/{self.model}",
            contents="ping",
            config=genai.types.GenerateContentConfig(
                max_output_tokens=1,
            ),
        )

    def _start_chat_session(self, system_prompt: str) -> None:
        if self.client is None:
            raise RuntimeError("Google AI API key is not configured")

        generation_config = genai.types.GenerateContentConfig(
            temperature=GENERATION_OPTIONS.get("temperature", 1.0),
            top_p=GENERATION_OPTIONS.get("top_p", 0.95),
            top_k=GENERATION_OPTIONS.get("top_k", 60),
            system_instruction=system_prompt,  # Set once, persists for session
            thinking_config=genai.types.ThinkingConfig(
                include_thoughts=False,
                thinking_level="minimal",
            ),
        )

        self._chat_session = self.client.chats.create(
            model=f"models/{self.model}",
            config=generation_config,
        )

    def _end_chat_session(self) -> None:
        self._chat_session = None

    def _send_chat_message(self, user_prompt: str, model: str) -> str:
        response = self._chat_session.send_message(user_prompt)
        return self._extract_text(response)

    @staticmethod
    def _extract_text(response: Any) -> str:
        response_dict = response.model_dump()
        candidates = response_dict.get("candidates", [])
        if not candidates:
            return ""

        candidate = candidates[0]
        content = candidate.get("content", {})
        parts = content.get("parts", [])

        for part in parts:
            if part.get("thought") is not True and part.get("text"):
                return part["text"]

        return ""


# ── Groq ─────────────────────────────────────────────────────────────────────

class GroqAIConnector(BaseAIConnector):
    """
    Talks to Groq's OpenAI-compatible API using stateful chat sessions.
    Maintains the messages array internally.
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = GROQ_MODEL,
        timeout: int = GROQ_TIMEOUT,
    ):
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"
        self.models_url = "https://api.groq.com/openai/v1/models"
        self._messages: list[dict[str, str]] = []
        super().__init__(
            api_key=api_key,
            model=model,
            timeout=timeout,
            log_dir_name="groq_api_responses",
        )

    def _is_configured(self) -> bool:
        return bool(self.api_key)

    def _do_validation_call(self) -> None:
        self._call_models()

    def _start_chat_session(self, system_prompt: str) -> None:
        self._messages = [
            {"role": "system", "content": system_prompt},
        ]

    def _end_chat_session(self) -> None:
        self._messages = []

    def _send_chat_message(self, user_prompt: str, model: str) -> str:
        self._messages.append({"role": "user", "content": user_prompt})

        payload = {
            "model": model,
            "messages": self._messages,
            "temperature": GENERATION_OPTIONS.get("temperature", 1.0),
            "top_p": GENERATION_OPTIONS.get("top_p", 0.95),
        }

        response_data = self._call_api(payload)
        text = self._extract_text(response_data)

        # Append assistant response to history for continuity
        self._messages.append({"role": "assistant", "content": text})

        # Trim history if it gets too long (keep last 20 turns)
        # System prompt + last 20 user/assistant pairs = ~41 messages max
        if len(self._messages) > 41:
            self._messages = [self._messages[0]] + self._messages[-40:]

        return text

    def _call_api(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        return self._request_json(
            url=self.base_url,
            method="POST",
            body=body,
            content_type="application/json",
        )

    def _call_models(self) -> dict[str, Any]:
        return self._request_json(
            url=self.models_url,
            method="GET",
        )

    def _request_json(
        self,
        url: str,
        method: str,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "Aimee/1.0 (+local)",
        }
        if content_type:
            headers["Content-Type"] = content_type

        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers=headers,
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                details = exc.read().decode("utf-8")
            except Exception:
                details = str(exc)

            lowered = details.lower()
            if "error code: 1010" in lowered:
                raise RuntimeError(
                    "HTTP 403 (Cloudflare 1010): request blocked before reaching Groq API. "
                    "Try without VPN/proxy, allow direct HTTPS to api.groq.com, and retry."
                ) from exc

            raise RuntimeError(f"HTTP {exc.code}: {details[:200]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(str(exc.reason)) from exc

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
        choices = response.get("choices", [])
        if not choices:
            return ""

        message = choices[0].get("message", {})
        content = message.get("content", "")
        return content if isinstance(content, str) else ""