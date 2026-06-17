"""
feedback_store.py
-----------------
Persistent storage for user feedback that must survive across sessions.

For now this holds only the banned-phrase list: lines the user explicitly
banned (🚫) are never spoken again, in this session or any future one.

Transient reactions (like / love / dislike) are NOT persisted here — they live
on the in-memory ``Turn`` objects and only influence prompts while the turn
stays inside the rolling ``BANNED_PHRASE_WINDOW``.
"""

from __future__ import annotations

import json
import logging
import threading

from config import BANNED_PHRASES_FILE

log = logging.getLogger(__name__)

_lock = threading.Lock()


def load_banned_phrases() -> list[str]:
    """Return the persisted banned phrases (empty list if none / unreadable)."""
    with _lock:
        return _load_unlocked()


def add_banned_phrase(phrase: str) -> bool:
    """
    Persist ``phrase`` to the banned list. Returns True if it was newly added,
    False if blank or already present.
    """
    phrase = (phrase or "").strip()
    if not phrase:
        return False
    with _lock:
        phrases = _load_unlocked()
        if phrase in phrases:
            return False
        phrases.append(phrase)
        _save_unlocked(phrases)
    log.info("Banned phrase added (%d total)", len(phrases))
    return True


def remove_banned_phrase(phrase: str) -> bool:
    """Remove ``phrase`` from the banned list. Returns True if it was present."""
    with _lock:
        phrases = _load_unlocked()
        if phrase not in phrases:
            return False
        phrases.remove(phrase)
        _save_unlocked(phrases)
    return True


# ── Internal (assume caller holds _lock) ─────────────────────────────────────

def _load_unlocked() -> list[str]:
    if not BANNED_PHRASES_FILE.exists():
        return []
    try:
        data = json.loads(BANNED_PHRASES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read banned phrases %s: %s", BANNED_PHRASES_FILE, exc)
        return []
    if isinstance(data, list):
        return [str(p) for p in data if str(p).strip()]
    return []


def _save_unlocked(phrases: list[str]) -> None:
    BANNED_PHRASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    BANNED_PHRASES_FILE.write_text(
        json.dumps(phrases, indent=2, ensure_ascii=False), encoding="utf-8"
    )
