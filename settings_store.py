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

from config import APP_CONFIG_DIR, SETTINGS_FILE

DEFAULT_SETTINGS: dict[str, Any] = {
    "google_api_key": "",
    "groq_api_key": "",
    "google_model": "gemma-4-31b-it",
    "groq_model": "openai/gpt-oss-120b",
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
}


def _as_clean_text(value: Any) -> str:
    return str(value or "").strip()


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
    settings["google_api_key"] = _as_clean_text(settings.get("google_api_key"))
    settings["groq_api_key"] = _as_clean_text(settings.get("groq_api_key"))
    settings["google_model"] = _as_clean_text(settings.get("google_model"))
    settings["groq_model"] = _as_clean_text(settings.get("groq_model"))
    settings["google_validation"] = _normalized_validation(settings.get("google_validation"))
    settings["groq_validation"] = _normalized_validation(settings.get("groq_validation"))
    return settings


def load_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)

    settings.update(
        {
            "google_model": os.getenv("GOOGLE_MODEL", settings["google_model"]),
            "groq_model": os.getenv("GROQ_MODEL", settings["groq_model"]),
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
    return {
        "google": bool(str(settings.get("google_api_key", "") or "").strip()),
        "groq": bool(str(settings.get("groq_api_key", "") or "").strip()),
    }
