# OSSM Controller

A Flask web application that generates varied AI-driven session scripts for the OSSM device using Google and Groq Generative AI models.

## Prerequisites

- Python 3.10+
- A Google API key (for Gemini/Gemma models) or a Groq API key (for Llama, Qwen, etc.)

## Setup

```bash
pip install -r requirements.txt
python main.py
```

Open http://localhost:5000 in your browser.

## Settings

The app stores runtime API keys in a local settings file at `~/.config/aimee/settings.json`. The server falls back to environment variables on startup, and the Settings tab lets you update the saved values without editing source files.

The Settings tab includes:

- Google and Groq API key management with immediate validation on save
- Model selection per provider, unlocked when a valid key is present
- Fixed-name prompt download/upload
- Revert of all current prompt overrides back to base

## Prompt Storage

Prompts are split into two layers under `prompts/`:

- `prompts/base/` holds the immutable source files
- `prompts/current/` holds editable overrides

When the app reads a prompt file, it checks `current` first and falls back to `base` if no override exists. Uploading a prompt writes to `current` using the same filename, and revert deletes the matching current overrides so the app goes back to the base files.

Only filenames that already exist in `prompts/base/` are accepted for upload.

## Project Structure

```text
ossm_controller/
├── main.py               # Entry point
├── app_factory.py        # Flask app factory
├── routes.py             # All HTTP routes
├── config.py             # Central settings (env-overridable)
├── settings_store.py     # Local settings load/save helpers
├── prompt_store.py       # Base/current prompt resolution helpers
├── ai_connector.py       # Google & Groq AI client wrappers
├── brain.py              # Session orchestrator
├── prompt_builder.py     # Prompt construction + diversity strategies
├── response_parser.py    # JSON extraction from model output
├── session_manager.py    # In-memory session + device state tracking
├── pattern_loader.py     # Loads pattern definitions from patterns/
├── device_bridge.py      # WebSocket / Serial device communication
├── device_emulator.py    # Standalone OSSM firmware emulator
├── prompts/
│   ├── base/             # Immutable prompt templates, seeds, examples
│   └── current/          # Editable user overrides (empty by default)
├── patterns/             # One .json file per motion pattern
├── templates/
│   ├── index.html        # GUI shell
│   ├── tab_setup.html
│   ├── tab_manual.html
│   ├── tab_ai.html
│   ├── tab_settings.html
│   └── block_device_gauge.html
└── static/
    ├── css/
    │   └── style.css     # GUI styles
    └── js/
        ├── app.js        # Shared GUI logic
        ├── setup.js
        ├── manual.js
        ├── ai.js
        └── settings.js
```

## Running

```bash
python main.py
```

Open http://localhost:5000 in your browser.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GOOGLE_MODEL` | Default Google model | `gemma-4-31b-it` |
| `GROQ_MODEL` | Default Groq model | `openai/gpt-oss-120b` |
| `FLASK_PORT` | HTTP server port | `5000` |
| `FLASK_DEBUG` | Enable Flask debug mode | `true` |
| `DEVICE_WS_URL` | Default device WebSocket URL | `ws://localhost:8888` |

API keys are managed in app settings and stored in the local settings file managed by `settings_store.py`.
