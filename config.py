"""
config.py
---------
Central configuration for the OSSM Controller application.
"""

import os
import sys
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
# The app distinguishes two roots:
#
#   RESOURCE_DIR  read-only data shipped with the app (prompts, intents,
#                 built-in patterns, templates, static).  In a PyInstaller
#                 build these are unpacked into a temporary directory exposed as
#                 sys._MEIPASS; running from source they live next to this file.
#
#   DATA_DIR      everything the app writes at runtime (user prompt overrides,
#                 custom patterns, uploaded funscripts/videos, logs).  Running
#                 from source this is the repo root, so behaviour is unchanged.
#                 In a frozen build the bundle is read-only and wiped each run,
#                 so writes are redirected to the per-user config directory.
#
# Override DATA_DIR with the AIMEE_DATA_DIR environment variable if you want the
# writable data somewhere specific (handy for portable installs).
def _is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


APP_CONFIG_DIR = Path.home() / ".config" / "aimee"

if _is_frozen():
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    DATA_DIR = Path(os.getenv("AIMEE_DATA_DIR", str(APP_CONFIG_DIR)))
else:
    RESOURCE_DIR = Path(__file__).resolve().parent
    DATA_DIR = Path(os.getenv("AIMEE_DATA_DIR", str(RESOURCE_DIR)))

# Backward-compatible alias; points at the read-only resource root.
BASE_DIR = RESOURCE_DIR

# ── Read-only bundled data (RESOURCE_DIR) ────────────────────────────────────
PATTERNS_DIR   = RESOURCE_DIR / "patterns"
PROMPTS_DIR    = RESOURCE_DIR / "prompts"
INTENTS_DIR    = RESOURCE_DIR / "intents"
TEMPLATES_DIR  = RESOURCE_DIR / "templates"
STATIC_DIR     = RESOURCE_DIR / "static"
DEVICE_EMULATOR_SCRIPT = RESOURCE_DIR / "device_emulator.py"

BASE_PROMPTS_DIR = PROMPTS_DIR / "base"
PROMPT_FILE    = BASE_PROMPTS_DIR / "full_prompt.txt"
EXAMPLES_DIR   = BASE_PROMPTS_DIR / "examples" / "big"

PROMPT_SEEDS_DIR = BASE_PROMPTS_DIR / "seeds"
PERSONA_MOODS_FILE = PROMPT_SEEDS_DIR / "persona_moods.txt"
PACING_STRATEGIES_FILE = PROMPT_SEEDS_DIR / "pacing_strategies.txt"

PROMPT_TASKS_DIR = BASE_PROMPTS_DIR / "tasks"
USER_TURN_TASK_FILE = PROMPT_TASKS_DIR / "user_turn_task.txt"

# ── Writable user data (DATA_DIR / APP_CONFIG_DIR) ───────────────────────────
SETTINGS_FILE  = APP_CONFIG_DIR / "settings.json"
# Persistent, cross-session list of lines the user banned (🚫).
BANNED_PHRASES_FILE = APP_CONFIG_DIR / "banned_phrases.json"

CURRENT_PROMPTS_DIR = DATA_DIR / "prompts" / "current"
CUSTOM_PATTERNS_DIR = DATA_DIR / "patterns" / "custom"
FUNSCRIPT_DIR       = DATA_DIR / "patterns" / "funscripts"
VIDEOS_DIR          = DATA_DIR / "patterns" / "videos"
LOGS_DIR            = DATA_DIR / "logs"

# ── Buffer & timing ──────────────────────────────────────────────────────────
DISPLAY_INTERVAL = 10.0   # seconds between displayed turns
LOW_WATERMARK = 3         # request more when buffer <= 3
HIGH_WATERMARK = 10       # generate 10 turns per batch
GENERATOR_SLEEP = 2.0     # seconds between buffer checks

BIG_MODEL_MAX_RETRIES = int(os.getenv("BIG_MAX_RETRIES", "3"))
BIG_MODEL_RETRY_DELAY = int(os.getenv("BIG_RETRY_DELAY", "30"))

# ── Google Generative AI ────────────────────────────────────────────────────
GOOGLE_MODEL     = os.getenv("GOOGLE_MODEL",    "gemma-4-31b-it")
GOOGLE_TIMEOUT   = int(os.getenv("GOOGLE_TIMEOUT", "240"))
MODEL_OPTIONS    = [
    "gemma-4-26b-a4b-it",
    "gemma-4-31b-it",
]

# ── Groq Generative AI ──────────────────────────────────────────────────────
GROQ_MODEL       = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_TIMEOUT     = int(os.getenv("GROQ_TIMEOUT", "240"))
GROQ_MODEL_OPTIONS = [
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-prompt-guard-2-22m",
    "meta-llama/llama-prompt-guard-2-86m",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3-32b"
]

# ── OpenRouter ───────────────────────────────────────────────────────────────
# OpenRouter is OpenAI-compatible, so the connector is a sibling of the Groq
# one.  Only zero-cost ":free" models are listed: the free pool is rate limited
# to 20 requests/minute and 50 requests/day (1000/day once the account has ever
# purchased $10 of credit), which is the binding constraint on a long session --
# one generation call covers HIGH_WATERMARK turns.  Ordered best-first; edit
# freely, OpenRouter rotates the free pool without notice.
OPENROUTER_MODEL   = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
OPENROUTER_TIMEOUT = int(os.getenv("OPENROUTER_TIMEOUT", "240"))
OPENROUTER_MODEL_OPTIONS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "inclusionai/ling-3.0-flash:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "openai/gpt-oss-20b:free",
]

# Generation hyperparameters
GENERATION_OPTIONS = {
    "temperature":   float(os.getenv("GEN_TEMPERATURE",  "1.2")),
    "top_p":         float(os.getenv("GEN_TOP_P",        "0.90")),
    "top_k":         int(os.getenv("GEN_TOP_K",          "60")),
}

# ── Session defaults ─────────────────────────────────────────────────────────
DEFAULT_TURNS        = int(os.getenv("DEFAULT_TURNS", "5"))
BANNED_PHRASE_WINDOW = int(os.getenv("BANNED_PHRASE_WINDOW", "20"))

# ── Flask ─────────────────────────────────────────────────────────────────────
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "true").lower() == "true"
FLASK_PORT  = int(os.getenv("FLASK_PORT", "5000"))
FLASK_HOST  = os.getenv("FLASK_HOST", "0.0.0.0")

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── Kokoro TTS ───────────────────────────────────────────────────────────────
# The default voice is a weighted style-vector blend (75% af_bella, 25%
# af_nicole) saved as a .pt.  Kokoro's comma syntax averages voices evenly, so
# a weighted mix has to be baked into a file; load_single_voice() accepts any
# path ending in .pt.  Paired with speed 0.85 it gives the slower, breathier
# delivery that the RVC pass below re-timbres into the target voice.
_BLEND_VOICE = str(BASE_DIR / "RVC" / "voices" / "bella75_nicole25.pt")
KOKORO_VOICE = os.getenv("KOKORO_VOICE", _BLEND_VOICE if os.path.exists(_BLEND_VOICE) else "af_heart")
KOKORO_SPEED = float(os.getenv("KOKORO_SPEED", "0.85"))
KOKORO_DEVICE = os.getenv("KOKORO_DEVICE", "auto")

# ── RVC voice conversion (post-Kokoro) ───────────────────────────────────────
# Applio runs in its own venv (numpy 2.x, faiss, torchcrepe) which conflicts
# with this app's environment, so conversion happens in a persistent worker
# subprocess rather than in-process.  See rvc_client.py -- it cannot be named
# rvc.py, which would shadow Applio's own top-level `rvc` package.
#
# RVC is frame-synchronous: it changes timbre without changing duration, so the
# word timings and viseme track produced by Kokoro stay valid unchanged.
_RVC_DIR = BASE_DIR / "RVC"
RVC_ENABLED = os.getenv("RVC_ENABLED", "true").lower() == "true"
RVC_APPLIO_DIR = os.getenv("RVC_APPLIO_DIR", str(_RVC_DIR / "Applio"))
RVC_PYTHON = os.getenv("RVC_PYTHON", str(_RVC_DIR / "Applio" / ".venv" / "bin" / "python"))
RVC_MODEL = os.getenv("RVC_MODEL", str(_RVC_DIR / "lux" / "lux_300e_4800s.pth"))
RVC_INDEX = os.getenv("RVC_INDEX", str(_RVC_DIR / "lux" / "lux.index"))
RVC_PITCH = int(os.getenv("RVC_PITCH", "0"))          # semitones; >+3 thins out
RVC_INDEX_RATE = float(os.getenv("RVC_INDEX_RATE", "0.7"))
RVC_PROTECT = float(os.getenv("RVC_PROTECT", "0.33"))

# ── Device bridge ────────────────────────────────────────────────────────────
DEFAULT_DEVICE_WS_URL = os.getenv("DEVICE_WS_URL", "ws://localhost:8888")

AI_TO_DEVICE_PATTERN_MAP = {
    "stop": -1,
    "simple_stroke": 0,
    "teasing_and_pounding": 1,
    "robo_stroke": 2,
    "half_n_half": 3,
    "deeper": 4,
    "stop_n_go": 5,
    "insist": 6,
}

# ── Stash media server ─────────────────────────────────────────────────────
# Stash exposes a GraphQL API and serves scene streams + funscripts directly.
# These act as defaults; the live values are stored in the settings file and
# editable from the Settings tab.
STASH_URL     = os.getenv("STASH_URL", "")          # e.g. http://192.168.1.50:9999
STASH_API_KEY = os.getenv("STASH_API_KEY", "")      # Stash API key (Settings > Security)
STASH_TAG     = os.getenv("STASH_TAG", "playable")  # only scenes with this tag are playable

# Optional SOCKS5 proxy for reaching the Stash server (e.g. "127.0.0.1:2080").
STASH_PROXY_ENABLED = os.getenv("STASH_PROXY_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
STASH_PROXY_ADDRESS = os.getenv("STASH_PROXY_ADDRESS", "")

# When disabled, the AI is never offered the play_video intent and no random
# video interludes are injected — no video clips are ever played.
STASH_VIDEO_ENABLED = os.getenv("STASH_VIDEO_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")

# Probability (0.0-1.0) that any given turn is turned into a play_video interlude.
# The LLM rarely picks play_video on its own, so the orchestrator injects it at
# this rate to keep video clips appearing roughly this often.
VIDEO_CHANCE  = float(os.getenv("VIDEO_CHANCE", "0.10"))

# ── Coyote BLE ───────────────────────────────────────────────────────────────
COYOTE_BLE_NAME = os.getenv("COYOTE_BLE_NAME", "47L121000")
COYOTE_SOFT_LIMIT_A = int(os.getenv("COYOTE_SOFT_LIMIT_A", "100"))
COYOTE_SOFT_LIMIT_B = int(os.getenv("COYOTE_SOFT_LIMIT_B", "100"))
COYOTE_DEFAULT_FREQ_MS = int(os.getenv("COYOTE_DEFAULT_FREQ_MS", "100"))

# ── Buttplug / Intiface ──────────────────────────────────────────────────────
# Intiface Central's default WebSocket server address.
BUTTPLUG_WS_URL = os.getenv("BUTTPLUG_WS_URL", "ws://127.0.0.1:12345")
# Minimum vibration for a non-zero stroke position. Motors take 50-100ms to spin
# up, so a full 0->1->0 sweep at speed never quite reaches either end and turns
# to mush; raising the floor keeps fast pulses distinct at the cost of never
# fully stopping between strokes.
BUTTPLUG_VIBE_FLOOR = float(os.getenv("BUTTPLUG_VIBE_FLOOR", "0.0"))
