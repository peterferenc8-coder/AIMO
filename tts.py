"""
tts.py
----
Local Kokoro TTS integration with word-level timestamp extraction.

When the orchestrator gets a turn from the AI, it passes the speech text
through synthesize().  The function returns:
  - a path to the generated audio file (WAV)
  - a list of word-level timing objects
  - a list of viseme (mouth shape) events for avatar lip-sync

The UI uses the timing data to highlight each word exactly when it is
spoken, keeping text display and audio perfectly in sync.  The viseme
track drives the 3D avatar's mouth.
"""

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

# numpy / soundfile / kokoro are imported lazily inside synthesize() so the
# module (and its cache/config helpers) import cleanly in builds that ship
# without the optional TTS dependencies.  See requirements-tts.txt.

log = logging.getLogger(__name__)

# -- Configuration -----------------------------------------------------------
SAMPLE_RATE = 24_000
VOICE = os.getenv("KOKORO_VOICE", "af_heart")
KOKORO_SPEED = float(os.getenv("KOKORO_SPEED", "1.0"))
DEVICE = os.getenv("KOKORO_DEVICE", "auto")  # auto | cpu | cuda
AUDIO_CACHE_DIR = Path(tempfile.gettempdir()) / "aimee_tts"

# Lazy-import Kokoro so the app can start even if the package is not installed.
_pipeline = None
_pipeline_device: str | None = None


def configure(voice: str | None = None, speed: float | None = None,
              device: str | None = None) -> None:
    """Update default voice/speed/device from settings (live)."""
    global VOICE, KOKORO_SPEED, DEVICE, _pipeline, _pipeline_device
    if voice:
        VOICE = str(voice)
    if speed is not None:
        try:
            KOKORO_SPEED = float(speed)
        except (TypeError, ValueError):
            pass
    if device:
        new_device = str(device).lower()
        if new_device != DEVICE:
            DEVICE = new_device
            # Force the pipeline to rebuild on the new device next synthesis.
            _pipeline = None
            _pipeline_device = None


def _resolve_device() -> str:
    if DEVICE == "cpu":
        return "cpu"
    if DEVICE == "cuda":
        return "cuda"
    return "cuda" if _cuda_available() else "cpu"


def _get_pipeline():
    """Lazy initialiser for the Kokoro KPipeline."""
    global _pipeline, _pipeline_device
    if _pipeline is not None:
        return _pipeline

    try:
        from kokoro import KPipeline
    except ImportError as exc:
        raise RuntimeError(
            "Text-to-speech is not installed. It is not bundled in the prebuilt "
            "binary; run from source and install the TTS extras with: "
            "pip install -r requirements-tts.txt"
        ) from exc

    device = _resolve_device()
    _pipeline = KPipeline(lang_code="a", device=device)
    _pipeline_device = device
    log.info("Kokoro pipeline initialised on %s (voice=%s)", device, VOICE)
    return _pipeline


def _cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


# -- Public API --------------------------------------------------------------

def synthesize(text: str, voice: str | None = None, speed: float | None = None) -> dict[str, Any]:
    """
    Convert *text* to speech using local Kokoro.

    Returns a dict with keys:
        audio_url   -- relative URL the frontend can fetch (/api/tts/audio/<id>)
        audio_path  -- absolute filesystem path to the WAV file
        words       -- list of {"word", "phonemes", "start_ms", "end_ms"}
        visemes     -- list of {"t_ms", "dur_ms", "viseme", "weight"} for lip-sync
        duration_ms -- total audio duration in milliseconds

    The returned *words* list is already ordered and non-overlapping.
    """
    if not text or not text.strip():
        return {"audio_url": None, "audio_path": None, "words": [],
                "visemes": [], "duration_ms": 0}

    voice = voice or VOICE
    speed = speed if speed is not None else KOKORO_SPEED

    # Build a deterministic cache key so repeated identical sentences
    # do not re-synthesise.
    cache_key = _make_cache_key(text, voice, speed)
    cache_path = AUDIO_CACHE_DIR / f"{cache_key}.wav"
    cache_meta = AUDIO_CACHE_DIR / f"{cache_key}.json"

    if cache_path.exists() and cache_meta.exists():
        log.debug("TTS cache hit for key %s", cache_key)
        with cache_meta.open("r", encoding="utf-8") as fh:
            meta = json.load(fh)
        meta["audio_path"] = str(cache_path)
        meta["audio_url"] = f"/api/tts/audio/{cache_key}"
        return meta

    # -- Synthesis --------------------------------------------------------
    pipeline = _get_pipeline()
    results = list(pipeline(text, voice=voice, speed=speed, split_pattern=r"\n+"))

    # Concatenate audio chunks
    audio_chunks = []
    for result in results:
        if result.audio is not None:
            audio_chunks.append(result.audio)

    if not audio_chunks:
        log.warning("Kokoro produced no audio for: %s", text[:80])
        return {"audio_url": None, "audio_path": None, "words": [],
                "visemes": [], "duration_ms": 0}

    import numpy as np

    audio = np.concatenate(audio_chunks)

    # Write WAV
    try:
        import soundfile as sf
    except ImportError as exc:
        raise RuntimeError("soundfile is required for TTS output.  pip install soundfile") from exc

    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sf.write(str(cache_path), audio, SAMPLE_RATE)

    # -- Extract word timings from Kokoro tokens --------------------------
    words = _extract_word_timings(results)
    visemes = _build_visemes(words)
    duration_ms = round(len(audio) / SAMPLE_RATE * 1000)

    meta = {
        "audio_url": f"/api/tts/audio/{cache_key}",
        "audio_path": str(cache_path),
        "words": words,
        "visemes": visemes,
        "duration_ms": duration_ms,
    }

    with cache_meta.open("w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)

    log.info("TTS generated %d words, %d ms audio", len(words), duration_ms)
    return meta


def get_audio_path(cache_key: str) -> Path | None:
    """Return the filesystem path for a cached audio file, or None."""
    path = AUDIO_CACHE_DIR / f"{cache_key}.wav"
    return path if path.exists() else None


def list_cache() -> list[dict[str, Any]]:
    """Return metadata for every cached utterance (for admin/debug)."""
    items = []
    for meta_file in sorted(AUDIO_CACHE_DIR.glob("*.json")):
        try:
            with meta_file.open("r", encoding="utf-8") as fh:
                items.append(json.load(fh))
        except Exception:
            continue
    return items


def clear_cache() -> int:
    """Delete all cached audio and metadata.  Returns number of files removed."""
    count = 0
    for ext in ("*.wav", "*.json"):
        for f in AUDIO_CACHE_DIR.glob(ext):
            try:
                f.unlink()
                count += 1
            except OSError:
                pass
    log.info("TTS cache cleared (%d files)", count)
    return count


# -- Internals ---------------------------------------------------------------

# Bumped whenever the shape of the cached metadata changes, so stale sidecar
# JSON (e.g. pre-viseme entries) is never served back.  v2: added phonemes +
# viseme track.
_META_VERSION = 2


def _make_cache_key(text: str, voice: str, speed: float) -> str:
    """Deterministic, filesystem-safe cache key."""
    import hashlib
    raw = f"{text.strip()}|{voice}|{speed:.3f}|v{_META_VERSION}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    # sanitize voice name for filesystem safety
    safe_voice = re.sub(r"[^\w\-]+", "_", voice)
    return f"{safe_voice}_{digest}"


def _extract_word_timings(results) -> list[dict[str, Any]]:
    """
    Walk through every Kokoro result and its tokens, grouping tokens into
    words using the whitespace flag.  Produces word-level start/end times
    in milliseconds.
    """
    words: list[dict[str, Any]] = []
    chunk_offset_seconds = 0.0

    for result in results:
        tokens = getattr(result, "tokens", None) or []
        if not tokens:
            # No token data -- fall back to chunk-duration approximation
            audio = getattr(result, "audio", None)
            if audio is not None:
                chunk_dur = len(audio) / SAMPLE_RATE
                chunk_offset_seconds += chunk_dur
            continue

        current_text: list[str] = []
        current_phonemes: list[str] = []
        current_start: float | None = None
        current_end: float | None = None
        last_end = chunk_offset_seconds

        for token in tokens:
            text = getattr(token, "text", "")
            if not text:
                continue

            start_ts = getattr(token, "start_ts", None)
            end_ts = getattr(token, "end_ts", None)

            if current_start is None and start_ts is not None:
                current_start = chunk_offset_seconds + float(start_ts)
            if end_ts is not None:
                current_end = chunk_offset_seconds + float(end_ts)

            current_text.append(text)
            current_phonemes.append(getattr(token, "phonemes", "") or "")

            # whitespace flag means this token completes a word
            if getattr(token, "whitespace", ""):
                word_text = "".join(current_text).strip()
                if word_text:
                    start = current_start if current_start is not None else last_end
                    end = current_end if current_end is not None else start
                    words.append({
                        "word": word_text,
                        "phonemes": "".join(current_phonemes),
                        "start_ms": max(0, round(start * 1000)),
                        "end_ms": max(0, round(end * 1000)),
                    })
                    last_end = end

                current_text = []
                current_phonemes = []
                current_start = None
                current_end = None

        if current_text:
            word_text = "".join(current_text).strip()
            if word_text:
                start = current_start if current_start is not None else last_end
                end = current_end if current_end is not None else start
                words.append({
                    "word": word_text,
                    "phonemes": "".join(current_phonemes),
                    "start_ms": max(0, round(start * 1000)),
                    "end_ms": max(0, round(end * 1000)),
                })

        # Advance offset by the audio length of this result chunk
        audio = getattr(result, "audio", None)
        if audio is not None:
            chunk_offset_seconds += len(audio) / SAMPLE_RATE

    # Post-process: ensure monotonic, non-overlapping, and clamp
    words = _sanitize_timings(words)
    return words


# -- Viseme (lip-sync) generation --------------------------------------------
#
# Kokoro/misaki emit a *compressed* IPA where several diphthongs collapse to a
# single uppercase char (A=eɪ, I=aɪ, O=oʊ, W=aʊ, Y=ɔɪ).  The full US symbol set
# is misaki.en.US_VOCAB.  We fold that onto the five mouth shapes a VRM model
# exposes as standard expressions -- aa/ih/ou/ee/oh -- plus "sil" (closed).
#
# Note the VRM names follow Japanese vowels (a/i/u/e/o), so IPA "i" as in
# *see* maps to "ih", and IPA "ɛ" as in *bed* maps to "ee".  They are not the
# English letter names they look like.
_VISEME_MAP = {
    # diphthongs (Kokoro's single-char forms) -- mapped to their opening vowel
    "A": "ee", "I": "aa", "O": "oh", "W": "aa", "Y": "oh",
    # monophthongs
    "i": "ih", "u": "ou", "æ": "aa", "ɑ": "aa", "ɔ": "oh",
    "ə": "aa", "ɛ": "ee", "ɜ": "ee", "ɪ": "ih", "ʊ": "ou",
    "ʌ": "aa", "ᵊ": "aa", "ᵻ": "ih",
    # bilabials -- the mouth must actually close, this is what sells lip-sync
    "b": "sil", "p": "sil", "m": "sil",
    # labiodental / rounded consonants
    "f": "ou", "v": "ou", "w": "ou", "ɹ": "ou",
    "ʃ": "ou", "ʒ": "ou", "ʧ": "ou", "ʤ": "ou",
    # alveolar / dental -- narrow mouth
    "l": "ih", "n": "ih", "t": "ih", "d": "ih", "s": "ih", "z": "ih",
    "θ": "ih", "ð": "ih", "j": "ih", "ɾ": "ih", "ʔ": "ih",
    # velars / glottal -- slightly open
    "k": "aa", "ɡ": "aa", "ŋ": "aa", "h": "aa",
}

# Vowels hold longer than consonants; weighting the interpolation by these
# beats an even split noticeably, for about four lines of code.
_VOWELS = frozenset("AIOWYiuæɑɔəɛɜɪʊʌᵊᵻ")

# How wide the mouth opens for each phoneme class.  Consonants at full weight
# make the avatar look like it is chewing.
_VOWEL_WEIGHT = 1.0
_CONSONANT_WEIGHT = 0.45

# Stress marks carry no mouth shape.
_STRESS_MARKS = "ˈˌː"

MIN_VISEME_MS = 40


def _build_visemes(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Turn word-level phoneme strings + timings into a viseme track.

    Kokoro gives us timings per *token* (usually a word), not per phoneme, so
    each word's span is divided across its phonemes proportionally, weighting
    vowels heavier than consonants.  This is an approximation -- exact
    per-phoneme durations would mean reaching into the model's `pred_dur`
    tensor -- but at conversational speed it reads correctly, because
    neighbouring visemes blend on the client anyway.

    Returns a list of {"t_ms", "dur_ms", "viseme", "weight"} ordered by time.
    """
    visemes: list[dict[str, Any]] = []

    for w in words:
        phonemes = w.get("phonemes") or ""
        start_ms = w["start_ms"]
        end_ms = w["end_ms"]
        span = max(0, end_ms - start_ms)

        # Strip stress/length marks, then keep only symbols we can render.
        symbols = [c for c in phonemes if c not in _STRESS_MARKS]
        symbols = [c for c in symbols if c in _VISEME_MAP]

        if not symbols or span <= 0:
            # Punctuation, silence, or a word with no usable G2P -- close up.
            visemes.append({
                "t_ms": start_ms,
                "dur_ms": span,
                "viseme": "sil",
                "weight": 0.0,
            })
            continue

        weights = [
            _VOWEL_WEIGHT if c in _VOWELS else _CONSONANT_WEIGHT
            for c in symbols
        ]
        total = sum(weights)

        cursor = float(start_ms)
        for symbol, weight in zip(symbols, weights):
            dur = span * (weight / total)
            visemes.append({
                "t_ms": round(cursor),
                "dur_ms": round(dur),
                "viseme": _VISEME_MAP[symbol],
                "weight": round(weight, 3),
            })
            cursor += dur

    # Close the mouth after the final phoneme so it does not hang open.
    if visemes:
        last = visemes[-1]
        visemes.append({
            "t_ms": last["t_ms"] + last["dur_ms"],
            "dur_ms": 120,
            "viseme": "sil",
            "weight": 0.0,
        })

    return visemes


def _sanitize_timings(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Clean up edge cases:
      - negative or zero-duration words get a small default duration
      - overlapping words are clipped so end <= next start
      - gaps are left as-is (they represent pauses)
    """
    if not words:
        return words

    MIN_WORD_MS = 50  # minimum visible duration for a word

    out = []
    for i, w in enumerate(words):
        start = w["start_ms"]
        end = w["end_ms"]

        if end < start:
            end = start + MIN_WORD_MS
        if end - start < MIN_WORD_MS:
            end = start + MIN_WORD_MS

        # If this word would overlap the next one, clip it
        if i + 1 < len(words):
            next_start = words[i + 1]["start_ms"]
            if end > next_start:
                end = next_start

        out.append({
            "word": w["word"],
            "phonemes": w.get("phonemes", ""),
            "start_ms": start,
            "end_ms": end,
        })

    return out
