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

    settings["google_api_key"] = str(settings.get("google_api_key", "") or "").strip()
    settings["groq_api_key"] = str(settings.get("groq_api_key", "") or "").strip()
    settings["google_model"] = str(settings.get("google_model", "") or "").strip()
    settings["groq_model"] = str(settings.get("groq_model", "") or "").strip()

    google_validation = settings.get("google_validation")
    if not isinstance(google_validation, dict):
        google_validation = dict(DEFAULT_SETTINGS["google_validation"])
    settings["google_validation"] = {
        "ok": bool(google_validation.get("ok", False)),
        "message": str(google_validation.get("message", "Not validated yet")),
        "checked_at": google_validation.get("checked_at"),
    }

    groq_validation = settings.get("groq_validation")
    if not isinstance(groq_validation, dict):
        groq_validation = dict(DEFAULT_SETTINGS["groq_validation"])
    settings["groq_validation"] = {
        "ok": bool(groq_validation.get("ok", False)),
        "message": str(groq_validation.get("message", "Not validated yet")),
        "checked_at": groq_validation.get("checked_at"),
    }

    return settings


def save_settings(settings: dict[str, Any]) -> None:
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "google_api_key": str(settings.get("google_api_key", "") or "").strip(),
        "groq_api_key": str(settings.get("groq_api_key", "") or "").strip(),
        "google_model": str(settings.get("google_model", DEFAULT_SETTINGS["google_model"]) or "").strip(),
        "groq_model": str(settings.get("groq_model", DEFAULT_SETTINGS["groq_model"]) or "").strip(),
        "google_validation": settings.get("google_validation", DEFAULT_SETTINGS["google_validation"]),
        "groq_validation": settings.get("groq_validation", DEFAULT_SETTINGS["groq_validation"]),
    }

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
