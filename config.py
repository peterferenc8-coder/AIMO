"""
config.py
---------
Central configuration for the OSSM Controller application.
"""

import os
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent
PATTERNS_DIR   = BASE_DIR / "patterns"
PROMPTS_DIR    = BASE_DIR / "prompts"
APP_CONFIG_DIR = Path.home() / ".config" / "aimee"
SETTINGS_FILE  = APP_CONFIG_DIR / "settings.json"
BASE_PROMPTS_DIR = PROMPTS_DIR / "base"
CURRENT_PROMPTS_DIR = PROMPTS_DIR / "current"

PROMPT_FILE    = BASE_PROMPTS_DIR / "full_prompt.txt"
EXAMPLES_DIR   = BASE_PROMPTS_DIR / "examples" / "big"

PROMPT_SEEDS_DIR = BASE_PROMPTS_DIR / "seeds"
PERSONA_MOODS_FILE = PROMPT_SEEDS_DIR / "persona_moods.txt"
PACING_STRATEGIES_FILE = PROMPT_SEEDS_DIR / "pacing_strategies.txt"

PROMPT_TASKS_DIR = BASE_PROMPTS_DIR / "tasks"
USER_TURN_TASK_FILE = PROMPT_TASKS_DIR / "user_turn_task.txt"

# ── Intents ─────────────────────────────────────────────────────────────────
INTENTS_DIR = BASE_DIR / "intents"

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
KOKORO_VOICE = os.getenv("KOKORO_VOICE", "af_heart")
KOKORO_SPEED = float(os.getenv("KOKORO_SPEED", "1.0"))
KOKORO_DEVICE = os.getenv("KOKORO_DEVICE", "auto")

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
