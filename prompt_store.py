"""
prompt_store.py
--------------
Helpers for resolving prompt files from the immutable base tree and the
editable current overlay.
"""

from __future__ import annotations

from pathlib import Path

from config import BASE_PROMPTS_DIR, CURRENT_PROMPTS_DIR


def _normalize_relative_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        candidate = candidate.relative_to(BASE_PROMPTS_DIR)
    return candidate


def base_path(path: str | Path) -> Path:
    return BASE_PROMPTS_DIR / _normalize_relative_path(path)


def current_path(path: str | Path) -> Path:
    return CURRENT_PROMPTS_DIR / _normalize_relative_path(path)


def resolve_prompt_path(path: str | Path) -> Path:
    base = base_path(path)
    current = current_path(path)
    if current.exists():
        return current
    return base


def prompt_exists_in_base(path: str | Path) -> bool:
    return base_path(path).exists()


def list_base_prompt_names() -> list[str]:
    if not BASE_PROMPTS_DIR.exists():
        return []
    names: list[str] = []
    for file_path in sorted(BASE_PROMPTS_DIR.rglob("*")):
        if file_path.is_file():
            names.append(file_path.relative_to(BASE_PROMPTS_DIR).as_posix())
    return names


def write_current_prompt(path: str | Path, content: str) -> Path:
    destination = current_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


def delete_current_prompt(path: str | Path) -> bool:
    destination = current_path(path)
    if not destination.exists():
        return False
    destination.unlink()
    return True


def clear_current_prompts() -> int:
    if not CURRENT_PROMPTS_DIR.exists():
        return 0

    removed = 0
    for file_path in sorted(CURRENT_PROMPTS_DIR.rglob("*"), reverse=True):
        if file_path.is_file():
            file_path.unlink()
            removed += 1

    for dir_path in sorted(CURRENT_PROMPTS_DIR.rglob("*"), reverse=True):
        if dir_path.is_dir() and not any(dir_path.iterdir()):
            dir_path.rmdir()

    return removed