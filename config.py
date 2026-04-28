"""
config.py
---------
Central configuration for the OSSM Controller application.
All magic numbers, paths, and tuneable settings live here.
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
SMALL_PROMPT_FILE = BASE_PROMPTS_DIR / "small_prompt.txt"
EXAMPLES_DIR   = BASE_PROMPTS_DIR / "examples" / "big"

PROMPT_SEEDS_DIR = BASE_PROMPTS_DIR / "seeds"
PERSONA_MOODS_FILE = PROMPT_SEEDS_DIR / "persona_moods.txt"
PACING_STRATEGIES_FILE = PROMPT_SEEDS_DIR / "pacing_strategies.txt"
OPENING_PATTERNS_FILE = PROMPT_SEEDS_DIR / "opening_patterns.txt"

PROMPT_RUNTIME_DIR = BASE_PROMPTS_DIR / "runtime"
SMALL_STATE_STOPPED_FILE = PROMPT_RUNTIME_DIR / "small_state_stopped.txt"
SMALL_STATE_MOVING_FILE = PROMPT_RUNTIME_DIR / "small_state_moving.txt"

PROMPT_TASKS_DIR = BASE_PROMPTS_DIR / "tasks"
USER_TURN_TASK_FILE = PROMPT_TASKS_DIR / "user_turn_task.txt"
SMALL_FALLBACK_TASK_FILE = PROMPT_TASKS_DIR / "small_fallback_task.txt"

SMALL_EXAMPLES_DIR = BASE_PROMPTS_DIR / "examples" / "small"


# ── Small / Fast Filler Model ────────────────────────────────────────────────
SMALL_MODEL = os.getenv("SMALL_MODEL", "gemma-3-12b-it")
DISPLAY_INTERVAL = 12.0   # seconds between displayed turns
LOW_WATERMARK = 3         # request more when buffer <= 3
HIGH_WATERMARK = 10       # generate 10 turns per batch
GENERATOR_SLEEP = 2.0     # seconds between buffer checks

BIG_MODEL_MAX_RETRIES = int(os.getenv("BIG_MAX_RETRIES", "3"))
BIG_MODEL_RETRY_DELAY = int(os.getenv("BIG_RETRY_DELAY", "30"))


# ── Google Generative AI (Gemini/Gemma) ─────────────────────────────────────
GOOGLE_MODEL     = os.getenv("GOOGLE_MODEL",    "gemma-4-31b-it")  # or "gemini-2.0-flash", "gemma-2-9b-it"
GOOGLE_TIMEOUT   = int(os.getenv("GOOGLE_TIMEOUT", "240"))   # seconds
MODEL_OPTIONS    = [
    "gemma-4-26b-a4b-it",
    "gemma-4-31b-it",
]

# ── Groq Generative AI ─────────────────────────────────────
GROQ_MODEL       = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b") 
 # or "groq-2.0-8k-it"
GROQ_TIMEOUT     = int(os.getenv("GROQ_TIMEOUT", "240"))   # seconds
GROQ_MODEL_OPTIONS      = [
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-prompt-guard-2-22m",
    "meta-llama/llama-prompt-guard-2-86m",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3-32b"
]


# Generation hyperparameters ─ raise temperature for more variety
# Note: Google API uses different parameter names/ranges than Ollama
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

# ── Logging ──────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── Device bridge ────────────────────────────────────────────────────────────
DEFAULT_DEVICE_WS_URL = os.getenv("DEVICE_WS_URL", "ws://localhost:8888")

AI_TO_DEVICE_PATTERN_MAP = {
    "stop": -1,                # handled specially
    "simple_stroke": 0,
    "teasing_and_pounding": 1,
    "robo_stroke": 2,
    "half_n_half": 3,
    "deeper": 4,
    "stop_n_go": 5,
    "insist": 6,
}