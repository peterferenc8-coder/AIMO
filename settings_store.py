"""
settings_store.py
-----------------
Load and save the app-wide local settings file.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from config import (
    APP_CONFIG_DIR,
    SETTINGS_FILE,
    STASH_URL,
    STASH_API_KEY,
    STASH_TAG,
    STASH_PROXY_ENABLED,
    STASH_PROXY_ADDRESS,
    STASH_VIDEO_ENABLED,
    VIDEO_CHANCE,
    GOOGLE_TIMEOUT,
    GROQ_TIMEOUT,
    OLLAMA_API_KEY,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    OLLAMA_TIMEOUT,
    OPENROUTER_MODEL,
    OPENROUTER_TIMEOUT,
    BIG_MODEL_MAX_RETRIES,
    BIG_MODEL_RETRY_DELAY,
    GENERATION_OPTIONS,
    DEFAULT_TURNS,
    BANNED_PHRASE_WINDOW,
    DISPLAY_INTERVAL,
    LOW_WATERMARK,
    HIGH_WATERMARK,
    GENERATOR_SLEEP,
    KOKORO_VOICE,
    KOKORO_SPEED,
    KOKORO_DEVICE,
    RVC_ENABLED,
    RVC_PITCH,
    RVC_INDEX_RATE,
    DEFAULT_DEVICE_WS_URL,
    BUTTPLUG_WS_URL,
    BUTTPLUG_VIBE_FLOOR,
    COYOTE_BLE_NAME,
    COYOTE_SOFT_LIMIT_A,
    COYOTE_SOFT_LIMIT_B,
    COYOTE_DEFAULT_FREQ_MS,
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "google_api_key": "",
    "groq_api_key": "",
    "openrouter_api_key": "",
    "google_model": "gemma-4-31b-it",
    "groq_model": "openai/gpt-oss-120b",
    "openrouter_model": OPENROUTER_MODEL,
    # ── Local OpenAI-compatible endpoint (Ollama, LM Studio, llama.cpp, vLLM) ─
    "ollama_base_url": OLLAMA_BASE_URL,
    "ollama_api_key": OLLAMA_API_KEY,   # optional; blank for a stock Ollama
    "ollama_model": OLLAMA_MODEL,
    # Model ids discovered from the server's /v1/models the last time Test
    # Connection ran.  Cached because model -> connector routing has to work
    # without a live round-trip on every generation call.
    "ollama_models": [],
    "tts_enabled": True,
    "stash_url": STASH_URL,
    "stash_api_key": STASH_API_KEY,
    "stash_tag": STASH_TAG,
    "stash_proxy_enabled": STASH_PROXY_ENABLED,
    "stash_proxy_address": STASH_PROXY_ADDRESS,
    "stash_video_enabled": STASH_VIDEO_ENABLED,
    "video_chance": VIDEO_CHANCE,
    # ── Generation / model tuning ──────────────────────────────────────────
    "gen_temperature": GENERATION_OPTIONS.get("temperature", 1.2),
    "gen_top_p": GENERATION_OPTIONS.get("top_p", 0.90),
    "gen_top_k": GENERATION_OPTIONS.get("top_k", 60),
    "google_timeout": GOOGLE_TIMEOUT,
    "groq_timeout": GROQ_TIMEOUT,
    "openrouter_timeout": OPENROUTER_TIMEOUT,
    "ollama_timeout": OLLAMA_TIMEOUT,
    "big_model_max_retries": BIG_MODEL_MAX_RETRIES,
    "big_model_retry_delay": BIG_MODEL_RETRY_DELAY,
    # ── Session / pacing ───────────────────────────────────────────────────
    "default_turns": DEFAULT_TURNS,
    "banned_phrase_window": BANNED_PHRASE_WINDOW,
    "display_interval": DISPLAY_INTERVAL,
    "low_watermark": LOW_WATERMARK,
    "high_watermark": HIGH_WATERMARK,
    "generator_sleep": GENERATOR_SLEEP,
    # ── Text-to-speech ─────────────────────────────────────────────────────
    "kokoro_voice": KOKORO_VOICE,
    "kokoro_speed": KOKORO_SPEED,
    "kokoro_device": KOKORO_DEVICE,
    # RVC re-timbres Kokoro output into the target voice.  Frame-synchronous,
    # so word timings and visemes are unaffected.
    "rvc_enabled": RVC_ENABLED,
    "rvc_pitch": RVC_PITCH,
    "rvc_index_rate": RVC_INDEX_RATE,
    # ── Device / hardware ──────────────────────────────────────────────────
    "device_ws_url": DEFAULT_DEVICE_WS_URL,
    "coyote_ble_name": COYOTE_BLE_NAME,
    "coyote_soft_limit_a": COYOTE_SOFT_LIMIT_A,
    "coyote_soft_limit_b": COYOTE_SOFT_LIMIT_B,
    "coyote_freq_ms": COYOTE_DEFAULT_FREQ_MS,
    "buttplug_ws_url": BUTTPLUG_WS_URL,
    "buttplug_vibe_floor": BUTTPLUG_VIBE_FLOOR,
    "google_validation": {
        "ok": False,
        "message": "Not validated yet",
        "checked_at": None,
    },
    "groq_validation": {
        "ok": False,
        "message": "Not validated yet",
        "checked_at": None,
    },
    "openrouter_validation": {
        "ok": False,
        "message": "Not validated yet",
        "checked_at": None,
    },
    "ollama_validation": {
        "ok": False,
        "message": "Not validated yet",
        "checked_at": None,
    },
    "stash_validation": {
        "ok": False,
        "message": "Not validated yet",
        "checked_at": None,
    },
}


def _as_clean_text(value: Any) -> str:
    return str(value or "").strip()


def _as_float(value: Any, fallback: float, lo: float | None = None, hi: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(fallback)
    if lo is not None:
        result = max(lo, result)
    if hi is not None:
        result = min(hi, result)
    return result


def _as_int(value: Any, fallback: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        result = int(fallback)
    if lo is not None:
        result = max(lo, result)
    if hi is not None:
        result = min(hi, result)
    return result


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for text in (str(item).strip() for item in value) if text]


def _normalized_validation(value: Any) -> dict[str, Any]:
    default = DEFAULT_SETTINGS["google_validation"]
    if not isinstance(value, dict):
        value = default
    return {
        "ok": bool(value.get("ok", default["ok"])),
        "message": str(value.get("message", default["message"])),
        "checked_at": value.get("checked_at", default["checked_at"]),
    }


def _normalize_settings(settings: dict[str, Any]) -> dict[str, Any]:
    d = DEFAULT_SETTINGS
    settings["google_api_key"] = _as_clean_text(settings.get("google_api_key"))
    settings["groq_api_key"] = _as_clean_text(settings.get("groq_api_key"))
    settings["openrouter_api_key"] = _as_clean_text(settings.get("openrouter_api_key"))
    settings["google_model"] = _as_clean_text(settings.get("google_model"))
    settings["groq_model"] = _as_clean_text(settings.get("groq_model"))
    settings["openrouter_model"] = _as_clean_text(settings.get("openrouter_model"))
    # The base URL is only tidied here (whitespace, trailing slash); turning it
    # into an API root is OllamaAIConnector's job, so the field keeps showing
    # whatever the user actually typed.
    settings["ollama_base_url"] = _as_clean_text(settings.get("ollama_base_url")).rstrip("/")
    settings["ollama_api_key"] = _as_clean_text(settings.get("ollama_api_key"))
    settings["ollama_model"] = _as_clean_text(settings.get("ollama_model"))
    settings["ollama_models"] = _as_str_list(settings.get("ollama_models"))
    settings["tts_enabled"] = bool(settings.get("tts_enabled", d["tts_enabled"]))
    settings["stash_url"] = _as_clean_text(settings.get("stash_url")).rstrip("/")
    settings["stash_api_key"] = _as_clean_text(settings.get("stash_api_key"))
    settings["stash_tag"] = _as_clean_text(settings.get("stash_tag"))
    settings["stash_proxy_enabled"] = bool(settings.get("stash_proxy_enabled", d["stash_proxy_enabled"]))
    settings["stash_proxy_address"] = _as_clean_text(settings.get("stash_proxy_address"))
    settings["stash_video_enabled"] = bool(settings.get("stash_video_enabled", d["stash_video_enabled"]))
    settings["video_chance"] = _as_float(settings.get("video_chance"), d["video_chance"], 0.0, 1.0)

    # Generation / model tuning
    settings["gen_temperature"] = _as_float(settings.get("gen_temperature"), d["gen_temperature"], 0.0, 2.0)
    settings["gen_top_p"] = _as_float(settings.get("gen_top_p"), d["gen_top_p"], 0.0, 1.0)
    settings["gen_top_k"] = _as_int(settings.get("gen_top_k"), d["gen_top_k"], 0, 1000)
    settings["google_timeout"] = _as_int(settings.get("google_timeout"), d["google_timeout"], 1, 3600)
    settings["groq_timeout"] = _as_int(settings.get("groq_timeout"), d["groq_timeout"], 1, 3600)
    settings["openrouter_timeout"] = _as_int(settings.get("openrouter_timeout"), d["openrouter_timeout"], 1, 3600)
    settings["ollama_timeout"] = _as_int(settings.get("ollama_timeout"), d["ollama_timeout"], 1, 3600)
    settings["big_model_max_retries"] = _as_int(settings.get("big_model_max_retries"), d["big_model_max_retries"], 0, 20)
    settings["big_model_retry_delay"] = _as_int(settings.get("big_model_retry_delay"), d["big_model_retry_delay"], 0, 600)

    # Session / pacing
    settings["default_turns"] = _as_int(settings.get("default_turns"), d["default_turns"], 1, 1000)
    settings["banned_phrase_window"] = _as_int(settings.get("banned_phrase_window"), d["banned_phrase_window"], 0, 200)
    settings["display_interval"] = _as_float(settings.get("display_interval"), d["display_interval"], 0.5, 600.0)
    settings["low_watermark"] = _as_int(settings.get("low_watermark"), d["low_watermark"], 0, 1000)
    settings["high_watermark"] = _as_int(settings.get("high_watermark"), d["high_watermark"], 1, 1000)
    settings["generator_sleep"] = _as_float(settings.get("generator_sleep"), d["generator_sleep"], 0.1, 60.0)

    # Text-to-speech
    settings["kokoro_voice"] = _as_clean_text(settings.get("kokoro_voice")) or d["kokoro_voice"]
    settings["kokoro_speed"] = _as_float(settings.get("kokoro_speed"), d["kokoro_speed"], 0.1, 4.0)
    device = _as_clean_text(settings.get("kokoro_device")).lower() or d["kokoro_device"]
    settings["kokoro_device"] = device if device in ("auto", "cpu", "cuda") else d["kokoro_device"]

    # RVC voice conversion.  Pitch is clamped to +/-6 semitones: the model is
    # only well-conditioned over its training F0 range (~157-252 Hz here), and
    # transposing much past +3 makes the output thin and artifacty.
    settings["rvc_enabled"] = bool(settings.get("rvc_enabled", d["rvc_enabled"]))
    settings["rvc_pitch"] = _as_int(settings.get("rvc_pitch"), d["rvc_pitch"], -6, 6)
    settings["rvc_index_rate"] = _as_float(settings.get("rvc_index_rate"), d["rvc_index_rate"], 0.0, 1.0)

    # Device / hardware
    settings["device_ws_url"] = _as_clean_text(settings.get("device_ws_url")) or d["device_ws_url"]
    settings["coyote_ble_name"] = _as_clean_text(settings.get("coyote_ble_name")) or d["coyote_ble_name"]
    settings["coyote_soft_limit_a"] = _as_int(settings.get("coyote_soft_limit_a"), d["coyote_soft_limit_a"], 0, 200)
    settings["coyote_soft_limit_b"] = _as_int(settings.get("coyote_soft_limit_b"), d["coyote_soft_limit_b"], 0, 200)
    settings["coyote_freq_ms"] = _as_int(settings.get("coyote_freq_ms"), d["coyote_freq_ms"], 10, 1000)
    settings["buttplug_ws_url"] = _as_clean_text(settings.get("buttplug_ws_url")) or d["buttplug_ws_url"]
    settings["buttplug_vibe_floor"] = _as_float(
        settings.get("buttplug_vibe_floor"), d["buttplug_vibe_floor"], 0.0, 0.9)

    settings["google_validation"] = _normalized_validation(settings.get("google_validation"))
    settings["groq_validation"] = _normalized_validation(settings.get("groq_validation"))
    settings["openrouter_validation"] = _normalized_validation(settings.get("openrouter_validation"))
    settings["ollama_validation"] = _normalized_validation(settings.get("ollama_validation"))
    settings["stash_validation"] = _normalized_validation(settings.get("stash_validation"))
    return settings


def load_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)

    settings.update(
        {
            "google_model": os.getenv("GOOGLE_MODEL", settings["google_model"]),
            "groq_model": os.getenv("GROQ_MODEL", settings["groq_model"]),
            "openrouter_model": os.getenv("OPENROUTER_MODEL", settings["openrouter_model"]),
            "ollama_model": os.getenv("OLLAMA_MODEL", settings["ollama_model"]),
        }
    )

    if SETTINGS_FILE.exists():
        try:
            with SETTINGS_FILE.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                settings.update({k: v for k, v in loaded.items() if k in settings})
        except (OSError, json.JSONDecodeError):
            pass
    return _normalize_settings(settings)


def save_settings(settings: dict[str, Any]) -> None:
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    base = dict(DEFAULT_SETTINGS)
    base.update(settings)
    payload = _normalize_settings(base)

    fd, tmp_path = tempfile.mkstemp(prefix="settings_", suffix=".json", dir=APP_CONFIG_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        Path(tmp_path).replace(SETTINGS_FILE)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def mask_secret(value: str | None) -> str:
    secret = (value or "").strip()
    if not secret:
        return ""
    visible = secret[:5]
    return f"{visible}..."


def provider_presence(settings: dict[str, Any]) -> dict[str, bool]:
    """Whether each back end has the credential it needs to be usable at all.
    For the local endpoint that credential is the base URL, not a key."""
    return {
        "google": bool(str(settings.get("google_api_key", "") or "").strip()),
        "groq": bool(str(settings.get("groq_api_key", "") or "").strip()),
        "openrouter": bool(str(settings.get("openrouter_api_key", "") or "").strip()),
        "ollama": bool(str(settings.get("ollama_base_url", "") or "").strip()),
    }
