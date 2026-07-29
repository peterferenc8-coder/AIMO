"""
ai_connector.py
---------------
Thin wrappers around the Google, Groq, OpenRouter and local (Ollama /
OpenAI-compatible) Generative AI APIs.

Now stateful: system prompt is set once per session, then only
new user messages are sent each turn.
"""

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

import google.genai as genai

from config import (
    GENERATION_OPTIONS,
    GOOGLE_MODEL,
    GOOGLE_TIMEOUT,
    GROQ_MODEL,
    GROQ_TIMEOUT,
    LOGS_DIR,
    OLLAMA_API_KEY,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT,
    OPENROUTER_MODEL,
    OPENROUTER_TIMEOUT,
)

log = logging.getLogger(__name__)


# ── Base connector ───────────────────────────────────────────────────────────

class BaseAIConnector:
    """
    Shared machinery for API health tracking, validation, and response logging.
    """

    # Shown when the back end lacks whatever it needs to be usable at all.
    # Overridden by back ends whose credential is not an API key.
    NOT_CONFIGURED_MESSAGE = "API key not configured"

    def __init__(self, *, api_key: str, model: str, timeout: int, log_dir_name: str,
                 gen_options: dict | None = None):
        self.api_key = ""
        self.model = model
        self.timeout = timeout
        self.gen_options = dict(gen_options) if gen_options else dict(GENERATION_OPTIONS)
        self.response_log_dir = LOGS_DIR / log_dir_name

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
        gen_options: dict | None = None,
    ) -> None:
        """Update connection settings in place without replacing callers."""
        if api_key is not None:
            self.api_key = api_key.strip()
        if model is not None:
            self.model = model
        if timeout is not None:
            self.timeout = timeout
        if gen_options is not None:
            self.gen_options = dict(gen_options)

    def health_check(self) -> dict[str, Any]:
        configured = self._is_configured()
        return {
            "ok": False if not configured else (True if self._last_api_ok is None else self._last_api_ok),
            "message": self.NOT_CONFIGURED_MESSAGE if not configured else self._last_api_message,
            "model": self.model,
            "checked_at": self._last_api_checked_at,
            "session_active": self._session_active,
        }

    def validate_api_key(self) -> dict[str, Any]:
        if not self._is_configured():
            self._mark_unhealthy(ValueError(self.NOT_CONFIGURED_MESSAGE))
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
        # Generous enough for the hand-written provider hints (quota advice, "is
        # the server running", the list of models actually installed) to survive
        # intact -- at 100 they were being cut mid-sentence.
        self._last_api_message = str(exc)[:300]
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
        gen_options: dict | None = None,
    ):
        self.client = None
        self._chat_session = None
        super().__init__(
            api_key=api_key,
            model=model,
            timeout=timeout,
            log_dir_name="google_api_responses",
            gen_options=gen_options,
        )

    def reconfigure(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        gen_options: dict | None = None,
    ) -> None:
        super().reconfigure(api_key=api_key, model=model, timeout=timeout, gen_options=gen_options)
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
            temperature=self.gen_options.get("temperature", 1.0),
            top_p=self.gen_options.get("top_p", 0.95),
            top_k=self.gen_options.get("top_k", 60),
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


# ── OpenAI-compatible back ends (Groq, OpenRouter) ───────────────────────────

class OpenAICompatConnector(BaseAIConnector):
    """
    Talks to an OpenAI-compatible /chat/completions endpoint using stateful
    chat sessions. Maintains the messages array internally.

    Subclasses supply the endpoints and, where the provider needs them, extra
    request headers or a friendlier hint for a provider-specific HTTP error.
    """

    BASE_URL = ""
    VALIDATION_URL = ""
    LOG_DIR_NAME = "openai_compat_api_responses"

    # System prompt + last 20 user/assistant pairs.
    MAX_HISTORY = 41

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        timeout: int = 240,
        gen_options: dict | None = None,
    ):
        self.base_url = self.BASE_URL
        self.validation_url = self.VALIDATION_URL
        self._messages: list[dict[str, str]] = []
        super().__init__(
            api_key=api_key,
            model=model,
            timeout=timeout,
            log_dir_name=self.LOG_DIR_NAME,
            gen_options=gen_options,
        )

    # ── Provider hooks ────────────────────────────────────────────────────────

    def _extra_headers(self) -> dict[str, str]:
        """Additional headers merged into every request."""
        return {}

    def _http_error_hint(self, status: int, details: str) -> str | None:
        """Return a friendlier message for a known provider-specific failure."""
        return None

    def _url_error_hint(self, reason: Any) -> str | None:
        """Return a friendlier message for a transport-level failure (DNS,
        connection refused, TLS).  Mostly of interest to back ends whose host is
        user-supplied, where a refused connection is the common first failure."""
        return None

    def _is_configured(self) -> bool:
        return bool(self.api_key)

    def _do_validation_call(self) -> None:
        self._call_validation()

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
            "temperature": self.gen_options.get("temperature", 1.0),
            "top_p": self.gen_options.get("top_p", 0.95),
        }

        response_data = self._call_api(payload)
        text = self._extract_text(response_data)

        # Append assistant response to history for continuity
        self._messages.append({"role": "assistant", "content": text})

        # Trim history if it gets too long (keep last 20 turns)
        if len(self._messages) > self.MAX_HISTORY:
            self._messages = [self._messages[0]] + self._messages[-(self.MAX_HISTORY - 1):]

        return text

    def _call_api(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        return self._request_json(
            url=self.base_url,
            method="POST",
            body=body,
            content_type="application/json",
        )

    def _call_validation(self) -> dict[str, Any]:
        return self._request_json(
            url=self.validation_url,
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
            "Accept": "application/json",
            "User-Agent": "Aimee/1.0 (+local)",
        }
        # Only sent when there is a key: a local endpoint typically wants no
        # Authorization header at all, and the hosted back ends would reject an
        # empty bearer token either way.
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self._extra_headers())
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

            hint = self._http_error_hint(exc.code, details)
            if hint:
                raise RuntimeError(hint) from exc

            raise RuntimeError(f"HTTP {exc.code}: {details[:200]}") from exc
        except urllib.error.URLError as exc:
            hint = self._url_error_hint(exc.reason)
            raise RuntimeError(hint or str(exc.reason)) from exc

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
        choices = response.get("choices", [])
        if not choices:
            return ""

        message = choices[0].get("message", {})
        content = message.get("content", "")
        return content if isinstance(content, str) else ""


# ── Groq ─────────────────────────────────────────────────────────────────────

class GroqAIConnector(OpenAICompatConnector):
    """Groq's OpenAI-compatible API."""

    BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
    VALIDATION_URL = "https://api.groq.com/openai/v1/models"
    LOG_DIR_NAME = "groq_api_responses"

    def __init__(
        self,
        api_key: str = "",
        model: str = GROQ_MODEL,
        timeout: int = GROQ_TIMEOUT,
        gen_options: dict | None = None,
    ):
        super().__init__(api_key=api_key, model=model, timeout=timeout, gen_options=gen_options)

    def _http_error_hint(self, status: int, details: str) -> str | None:
        if "error code: 1010" in details.lower():
            return (
                "HTTP 403 (Cloudflare 1010): request blocked before reaching Groq API. "
                "Try without VPN/proxy, allow direct HTTPS to api.groq.com, and retry."
            )
        return None


# ── OpenRouter ───────────────────────────────────────────────────────────────

class OpenRouterAIConnector(OpenAICompatConnector):
    """
    OpenRouter's OpenAI-compatible API.

    Two deliberate differences from Groq:

    * Validation hits ``/api/v1/key`` rather than ``/api/v1/models`` -- the
      models listing is public and answers 200 to an unauthenticated request,
      so it would happily "validate" a garbage key.
    * ``HTTP-Referer``/``X-Title`` are OpenRouter's app-attribution headers.

    Only ``temperature`` and ``top_p`` are sent (as for Groq).  ``top_k`` is
    deliberately omitted: several free models -- Nemotron 3 Ultra among them --
    do not accept it.
    """

    BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
    VALIDATION_URL = "https://openrouter.ai/api/v1/key"
    LOG_DIR_NAME = "openrouter_api_responses"

    def __init__(
        self,
        api_key: str = "",
        model: str = OPENROUTER_MODEL,
        timeout: int = OPENROUTER_TIMEOUT,
        gen_options: dict | None = None,
    ):
        super().__init__(api_key=api_key, model=model, timeout=timeout, gen_options=gen_options)

    def _extra_headers(self) -> dict[str, str]:
        return {
            "HTTP-Referer": "https://github.com/peterferenc8-coder/AIMO",
            "X-Title": "AIMO",
        }

    def _http_error_hint(self, status: int, details: str) -> str | None:
        if status == 429:
            return (
                "HTTP 429: OpenRouter free-tier rate limit hit (20 requests/minute, and "
                "50/day until the account has purchased $10 of credit -- then 1000/day). "
                "Wait, lower the turn rate, or switch back to Groq/Google."
            )
        if status == 402:
            return (
                "HTTP 402: OpenRouter reports insufficient credit. Free models should not "
                "charge -- check the selected model still ends in ':free'."
            )
        return None


# ── Local OpenAI-compatible endpoint (Ollama & friends) ──────────────────────

class OllamaAIConnector(OpenAICompatConnector):
    """
    A locally hosted OpenAI-compatible server.  Ollama is the reference target,
    but LM Studio, llama.cpp's ``server`` and vLLM all speak the same dialect and
    work unchanged -- only the base URL differs.

    Three differences from the hosted siblings:

    * **No API key.**  ``_is_configured`` keys off the base URL instead, and the
      ``Authorization`` header is only sent when a key happens to be set (vLLM's
      ``--api-key``, or a reverse proxy in front of Ollama).
    * **User-supplied base URL**, so it is normalised: a scheme is added when
      missing, a trailing slash or pasted ``/chat/completions`` is stripped, and
      ``/v1`` is appended when absent.  ``http://localhost:11434`` and
      ``http://localhost:11434/v1`` therefore both work.
    * **No curated model list.**  A local server offers whatever has been pulled
      onto the box, so ``list_models()`` reads ``GET /v1/models`` and validation
      additionally checks that the selected model is actually installed --
      otherwise the first real generation call would be the thing that
      discovered the typo.  The discovered names are left on
      ``available_models`` so a caller can cache them without a second request.
    """

    LOG_DIR_NAME = "ollama_api_responses"
    NOT_CONFIGURED_MESSAGE = "No endpoint URL configured"

    def __init__(
        self,
        api_key: str = OLLAMA_API_KEY,
        model: str = OLLAMA_MODEL,
        timeout: int = OLLAMA_TIMEOUT,
        gen_options: dict | None = None,
        base_url: str = OLLAMA_BASE_URL,
    ):
        # Set before super().__init__(), which reaches reconfigure() below.
        self.root_url = ""
        self.available_models: list[str] = []
        super().__init__(api_key=api_key, model=model, timeout=timeout, gen_options=gen_options)
        self.reconfigure(base_url=base_url)

    def reconfigure(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        gen_options: dict | None = None,
        base_url: str | None = None,
    ) -> None:
        super().reconfigure(api_key=api_key, model=model, timeout=timeout, gen_options=gen_options)
        if base_url is not None:
            self.root_url = self._normalize_base_url(base_url)
            self.base_url = f"{self.root_url}/chat/completions" if self.root_url else ""
            self.validation_url = f"{self.root_url}/models" if self.root_url else ""

    # ── Model discovery ───────────────────────────────────────────────────────

    def list_models(self) -> list[str]:
        """Model ids the server currently serves, sorted."""
        if not self.root_url:
            raise RuntimeError("No base URL configured")

        response = self._call_validation()
        entries = response.get("data") or []
        names = {
            str(entry["id"]).strip()
            for entry in entries
            if isinstance(entry, dict) and entry.get("id")
        }
        return sorted(names)

    # ── Provider hooks ────────────────────────────────────────────────────────

    def _is_configured(self) -> bool:
        return bool(self.root_url)

    def _do_validation_call(self) -> None:
        self.available_models = self.list_models()

        if not self.available_models:
            raise RuntimeError(
                f"Reached {self.root_url} but it serves no models "
                "(for Ollama, pull one first: `ollama pull llama3.1:8b`)"
            )

        if self.model and self.model not in self.available_models:
            preview = ", ".join(self.available_models[:5])
            more = " ..." if len(self.available_models) > 5 else ""
            raise RuntimeError(
                f"Server reachable, but '{self.model}' is not installed. "
                f"Available: {preview}{more}"
            )

    def _http_error_hint(self, status: int, details: str) -> str | None:
        if status == 404:
            return (
                f"HTTP 404 from {self.root_url}. Check the base URL points at the server "
                "root (e.g. http://localhost:11434) and that the model is installed "
                "(`ollama list`)."
            )
        if status in (401, 403):
            return (
                f"HTTP {status} from {self.root_url}: the endpoint wants authentication. "
                "Fill in the optional API key field."
            )
        return None

    def _url_error_hint(self, reason: Any) -> str | None:
        return (
            f"Cannot reach {self.root_url or 'the configured endpoint'} ({reason}). "
            "Is the server running? For Ollama, start it with `ollama serve` "
            "(or the desktop app) and confirm the port."
        )

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        """
        Turn whatever the user typed into the OpenAI-compatible API root.

        ``localhost:11434``, ``http://localhost:11434/``  and
        ``http://localhost:11434/v1/chat/completions`` all become
        ``http://localhost:11434/v1``.
        """
        cleaned = (url or "").strip().rstrip("/")
        if not cleaned:
            return ""

        if "://" not in cleaned:
            cleaned = f"http://{cleaned}"

        for suffix in ("/chat/completions", "/completions"):
            if cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)]
                break

        if not cleaned.endswith("/v1"):
            cleaned = f"{cleaned}/v1"

        return cleaned
