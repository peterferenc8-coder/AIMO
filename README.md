# AIMO — AI Session Orchestrator ("Aimee")

AIMO is a Flask web application that drives interactive hardware (an **OSSM** linear
actuator or a **DG-Lab Coyote 3.0** e-stim unit) from an AI persona. A large language
model (Google Gemini/Gemma or Groq-hosted models) generates a running stream of
*turns* — each turn is a line of speech plus a narrative **intent** and an
**intensity**. The orchestrator translates those intents into concrete device motion,
optionally speaks the text with local **Kokoro TTS**, and can cut to interactive video
clips streamed from a **Stash** media server (whose funscripts drive the device).

The browser UI is a tabbed control panel; all state lives in memory for the lifetime
of the Flask process.

---

## Table of contents

- [Features](#features)
- [Download & run](#download--run)
- [Quick start (from source)](#quick-start-from-source)
- [Building a release](#building-a-release)
- [Architecture](#architecture)
  - [The two-loop orchestrator](#the-two-loop-orchestrator)
  - [Lifecycle of a turn](#lifecycle-of-a-turn)
  - [The AI consumer / connectors](#the-ai-consumer--connectors)
  - [Prompt system](#prompt-system)
  - [User feedback](#user-feedback)
  - [Intents and pattern translation](#intents-and-pattern-translation)
  - [Devices](#devices)
  - [Stash integration](#stash-integration)
  - [Funscript player](#funscript-player)
  - [Text-to-speech](#text-to-speech)
- [Settings](#settings)
- [Prompt storage](#prompt-storage)
- [Authoring intents and patterns](#authoring-intents-and-patterns)
- [HTTP API](#http-api)
- [Project structure](#project-structure)
- [Configuration reference](#configuration-reference)
- [Dependencies](#dependencies)

---

## Features

- **AI-generated sessions** — a persona produces speech + intent + intensity per turn, paced and de-duplicated to avoid repetition.
- **Two AI back ends** — Google Generative AI (Gemini/Gemma) and Groq's OpenAI-compatible API, switchable per session.
- **Intent → motion compiler** — narrative intents (`tease`, `build`, `reward`, `settle`, `stop`) plus a 0.0–1.0 intensity are compiled to concrete device commands via per-intent JSON "bands" with weighted random variations.
- **Multiple devices** — OSSM (WebSocket or serial) and Coyote 3.0 (BLE), behind a common device abstraction with a live state stream.
- **Stash media server** — pull random tagged scenes, proxy their video through Flask (keeping the API key server-side), and drive the device from each scene's funscript. Optional SOCKS5 tunnelling.
- **Funscript playback** — upload/load `.funscript` files or play scene funscripts, with seek/pause/resume and latency/invert tuning.
- **Local TTS** — Kokoro synthesizes speech with word-level timing so the UI highlights each word as it is spoken.
- **Live feedback** — like / love / dislike / ban any displayed line; reactions are fed back into the prompt to steer the AI, and bans persist across sessions so a banned line is never spoken again.
- **Fully settable runtime config** — generation params, pacing/buffering, timeouts, TTS voice, device limits, and more, all editable from the Settings tab with a global save and per-service connectivity tests.
- **Built-in emulators** — a standalone OSSM firmware emulator and a one-click serial (PTY) emulator for development without hardware.

---

## Download & run

The easiest way to run AIMO is the **single-file binary** — no Python, no `pip`,
no setup. Grab the one for your platform from the
[Releases page](https://github.com/peterferenc8-coder/AIMO/releases):

| Platform | File |
|----------|------|
| Linux    | `AIMO-linux-x86_64` |
| Windows  | `AIMO-windows-x86_64.exe` |
| macOS    | `AIMO-macos-arm64` |

Then run it:

```bash
# Linux / macOS — mark executable once, then run
chmod +x AIMO-linux-x86_64
./AIMO-linux-x86_64
```

On Windows, just double-click `AIMO-windows-x86_64.exe`.

The app starts a local server and **opens your browser automatically** at
<http://localhost:5000>. To set up an AI back end, open the **Settings** tab, paste a
Google and/or Groq API key, **Save All**, then press **Test Connection**. Once a key
validates, its models unlock on the **AI Session** tab.

> **Where your data lives.** The binary is self-contained and read-only; everything
> you create or change at runtime — settings, custom patterns, uploaded
> funscripts/videos, prompt overrides, and logs — is written to `~/.config/aimee`
> (`%USERPROFILE%\.config\aimee` on Windows). Point it elsewhere with the
> `AIMEE_DATA_DIR` environment variable.

> **First launch warnings.** The binaries are unsigned. On macOS, clear the
> quarantine flag with `xattr -d com.apple.quarantine AIMO-macos-arm64` (or right-click
> → Open). On Windows, choose **More info → Run anyway** on the SmartScreen prompt.

---

## Quick start (from source)

```bash
pip install -r requirements.txt
python main.py
```

Open <http://localhost:5000> (set `AIMEE_OPEN_BROWSER=1` to auto-open it as the binary does).

`requirements.txt` covers the core (Flask, `google-genai`, `websockets`, `pyserial`)
plus the local TTS (Kokoro) and Coyote BLE extras. Running from source, all runtime
data stays in the repo working tree exactly as before.

To set up an AI back end, open the **Settings** tab, paste a Google and/or Groq API key,
**Save All**, then press **Test Connection**. Once a key validates, its models unlock for
selection on the **AI Session** tab.

---

## Building a release

Releases are built with [PyInstaller](https://pyinstaller.org/) from
[`aimo.spec`](aimo.spec), which bundles the interpreter, all dependencies, and the
read-only data (prompts, intents, built-in patterns, templates, static assets) into one
executable.

```bash
pip install -r requirements.txt pyinstaller
pyinstaller aimo.spec
# → dist/AIMO   (or dist/AIMO.exe on Windows)
```

Run the binary on the OS you want to ship for — PyInstaller does not cross-compile, so
each platform's binary must be built on that platform. The
[`.github/workflows/release.yml`](.github/workflows/release.yml) workflow does this
automatically: push a version tag and it builds Linux, Windows, and macOS binaries and
attaches them to a GitHub Release.

```bash
git tag v1.0.0
git push origin v1.0.0
```

> **Binary size & first start.** Bundling Kokoro pulls in PyTorch, so the executable is
> large (hundreds of MB) and a one-file build takes a few seconds to unpack on first run.
> Kokoro also downloads its voice model from Hugging Face on first synthesis, so initial
> TTS needs an internet connection.

---

## Architecture

```
                                  ┌──────────────────────────────────────────┐
   Browser (tabbed UI)            │            SessionOrchestrator             │
   ┌──────────────┐   HTTP/SSE    │                                            │
   │ setup/manual │ ─────────────▶│   generator loop ──┐      ┌── display loop │
   │ ai/funscript │               │   (producer)       │      │  (consumer)    │
   │ settings     │ ◀──── poll ───│        │            ▼      ▼                │
   └──────────────┘               │        │      ┌───────────────┐            │
                                  │        │      │ pending buffer│            │
        routes.py  ──────────────▶│        ▼      └───────────────┘            │
                                  │   AI connector       │                     │
                                  │   (Google / Groq)    │ every DISPLAY_      │
                                  │        │             │ INTERVAL seconds    │
                                  │        ▼             ▼                     │
                                  │   ResponseParser → IntentCompiler          │
                                  │        │             │                     │
                                  │        ▼             ▼                     │
                                  │   Brain/Session   DeviceState → device     │
                                  │   PromptBuilder      │      TTS synth       │
                                  └──────────────────────┼─────────────────────┘
                                                         ▼
                              OSSM (WS/serial) · Coyote (BLE) · Stash video/funscript
```

Key modules:

| Module | Role |
|--------|------|
| `main.py` / `app_factory.py` | Entry point and Flask app factory (logging, route registration, default device). |
| `routes.py` | Every HTTP endpoint; owns the single `SessionOrchestrator` instance and the `FunscriptPlayer`. |
| `orchestrator.py` | The heart: producer/consumer loops, session lifecycle, settings application. |
| `ai_connector.py` | Stateful wrappers around Google and Groq chat APIs. |
| `brain.py` / `prompt_builder.py` | Compose the system prompt, seed prompt, and per-turn prompts. |
| `response_parser.py` | Extract structured turns (speech/intent/intensity) from raw model text. |
| `intent_compiler.py` | Map `(intent, intensity)` → concrete device command. |
| `session_manager.py` | In-memory turn history + effective device state. |
| `feedback_store.py` | Persistent banned-phrase store for cross-session feedback (🚫). |
| `devices/` | Device abstraction (`base`), implementations (`ossm`, `coyote_ble`), and a `registry` singleton. |
| `stash_client.py` | GraphQL + media accessor for a Stash server, with stdlib SOCKS5 support. |
| `funscript_player.py` | Schedules funscript actions and streams positions to the device. |
| `tts.py` | Kokoro TTS with word-level timing and an on-disk cache. |
| `settings_store.py` | Load/normalize/save the local settings file (the single source of truth for runtime config). |

### The two-loop orchestrator

`SessionOrchestrator` runs a **producer/consumer** pattern with watermark backpressure:

- **Generator loop (producer)** — when the pending buffer drops to or below `LOW_WATERMARK`, it asks the big model for a batch of `HIGH_WATERMARK` turns on a background thread, with adaptive backoff after failures.
- **Display loop (consumer)** — pops one item from the buffer every `DISPLAY_INTERVAL` seconds, applies its device command, and records it as "displayed" for the UI to poll. A video turn instead holds the loop until the clip ends (or a safety timeout).

The browser drives the UI by polling `GET /api/poll?since=<index>`, which returns any newly displayed items. Pause/resume/clear act on the shared state under a lock.

### Lifecycle of a turn

1. **Generate** — the big connector returns raw text (a JSON list of turn objects).
2. **Parse** — `ResponseParser` strips markdown fences, tolerates NDJSON / partial JSON, and yields `Turn` objects with `speech`, `intent`, and `ai_intensity` (clamped to `[0,1]`).
3. **Maybe inject video** — with probability `video_chance`, a normal turn is promoted to a `play_video` interlude (only when Stash is configured).
4. **Compile** — for a motion turn, `IntentCompiler.compile(intent, intensity)` selects an intensity band and renders a `CompiledCommand` (pattern, speed, depth, base, duration), which becomes the turn's `Commands`.
5. **Record** — the turn is appended to `Brain`/`SessionManager` history (used for the "do not repeat" window) and the running `DeviceState`.
6. **Build display item** — speech is synthesized via TTS (if enabled), or a Stash clip is resolved for a video turn.
7. **Display** — the consumer loop applies the command to the active device and exposes the item to the UI.

### The AI consumer / connectors

`ai_connector.py` defines a `BaseAIConnector` with two concrete subclasses:

- **`GoogleAIConnector`** — uses `google-genai` chat sessions; the system prompt is set once via `start_session`, then each turn sends only a small user prompt.
- **`GroqAIConnector`** — talks to Groq's OpenAI-compatible REST endpoint using only the stdlib, maintaining the message history internally and trimming it to a bounded window.

Both are **stateful** (system prompt sent once per session), expose `validate_api_key()` / `health_check()`, and carry their own `gen_options` (temperature/top-p/top-k) and `timeout`, which the orchestrator pushes from settings via `reconfigure()`. Failed big-model calls are retried up to `big_model_max_retries` times with `big_model_retry_delay` between attempts (interruptible by a stop request).

### Prompt system

`Brain` is the "creative director"; `PromptBuilder` assembles the actual text:

- **System prompt** — persona definition + the patterns block (`PatternLoader.to_prompt_block()`) + state info, sent once at session start.
- **Seed prompt** — the first user message: a randomly chosen persona *mood*, *pacing strategy*, and opening pattern, drawn from the seed files.
- **Per-turn prompt** — minimal fresh context: any user event, the current device state, a **recent-speech window** (the last `banned_phrase_window` lines) used both to avoid repetition and to surface the user's reactions, plus a persistent **banned-phrases** block (see [User feedback](#user-feedback)).

### User feedback

Every displayed turn carries four reaction buttons — 👍 like, ❤️ love, 👎 dislike,
and 🚫 ban — that let you steer the AI in real time. Clicking a button posts to
`POST /api/feedback {index, reaction}`; the orchestrator maps the display index back to
the matching `Turn` (display and history share one FIFO order). Clicking an active button
again clears it (`clear`/`unban`).

Reactions wire back into the prompt in two ways:

- **Transient (like / love / dislike)** — attached to the in-memory `Turn`. While that
  turn stays inside the rolling recent-speech window, its reaction is shown to the model
  in full (unreacted lines stay truncated) with a note to lean into liked/loved styles or
  steer away from disliked ones. The reaction ages out with the turn and dies with the
  session. like/love/dislike are mutually exclusive per turn.
- **Persistent (ban)** — the spoken line is written to `~/.config/aimee/banned_phrases.json`
  (`feedback_store.py`) and injected into every per-turn prompt as a hard "never say this
  again" constraint, in this session and all future ones, until un-banned. A ban is
  independent of the transient reaction, so a line can be both disliked *and* banned.

### Intents and pattern translation

There are two related but distinct concepts:

- **Patterns** (`patterns/*.json`) — motion primitives the device firmware understands (e.g. `simple_stroke`, `half_n_half`, `deeper`, `stop_n_go`). Each file documents its parameters; they're serialized into the system prompt so the model knows what exists.
- **Intents** (`intents/<name>/*.json`) — *narrative* directives the AI emits (`tease`, `build`, `reward`, `settle`, plus the built-in `stop`). Each intent folder contains one JSON file per **intensity band** (e.g. `0.0-0.3.json`, `0.4-0.6.json`, `0.7-1.0.json`).

Compilation (`IntentCompiler`):

1. `stop` is built-in (halts the machine; no band file needed).
2. Otherwise the band whose `intensity_range` contains the (jittered) intensity is selected.
3. The band is *rendered*: its base parameters are taken, then one of its optional `variations` is chosen by weighted probability and merged on top.
4. A repeat-avoidance check re-rolls once with extra jitter if the resulting fingerprint matches a recent one.

The result is a `CompiledCommand` with a **pattern name**, `speed`, `depth`, `base`,
optional `intensity`/`easing`, and `duration_ms`.

When that command reaches the **OSSM** device (`OSSMDevice.apply_ai_commands`), motion
parameters are set first, then the pattern name is mapped to a firmware **slot index**
via `AI_TO_DEVICE_PATTERN_MAP` in `config.py`:

```
stop → -1   simple_stroke → 0   teasing_and_pounding → 1   robo_stroke → 2
half_n_half → 3   deeper → 4   stop_n_go → 5   insist → 6
```

— and the device is told `setPattern`/`startPattern`. (This map is intentionally a
fixed constant, since it's tied to firmware slots, not a user preference.)

### Devices

All devices implement `AbstractDevice` (`devices/base.py`): `connect`, `disconnect`,
`send_command`, `emergency_stop`, plus a `DeviceState` and a listener mechanism used to
stream live state to the browser over SSE. A `registry` keeps a single active device and
swaps implementations on request.

- **`OSSMDevice`** — connects over **WebSocket** (default `ws://localhost:8888`) or **serial** (auto-detected from a `/dev/…`, `COM…`, `tty…`, or `/tmp/…` address). It reconnects with exponential backoff and forwards raw position messages to listeners. A standalone `device_emulator.py` and a one-click serial-PTY emulator (`socat` + emulator) allow development without hardware.
- **`CoyoteBLE`** — direct BLE control of a DG-Lab Coyote 3.0 via `bleak`, implementing the V3 protocol. Channel strengths are clamped to configurable **soft limits**, and BLE name / frequency / limits are taken from settings (and pushed live to a connected device when saved).

### Stash integration

`stash_client.py` is a dependency-free client for a [Stash](https://stashapp.cc) server:

- **Discovery** — resolves the configured tag to an ID, then queries `findScenes` for every scene carrying that tag (`id`, `title`, `interactive`, duration). Interactive scenes are treated as having a funscript. Results are cached until the config changes.
- **Selection** — `pick_random_scene(prefer_interactive=True)` chooses a clip, preferring interactive ones so the funscript can drive the device.
- **Video proxying** — `GET /api/stash/video/<id>` streams the scene through Flask, forwarding `Range` headers so the browser can seek, and keeping the Stash **API key server-side** (never exposed to the page).
- **Funscript** — `fetch_funscript()` pulls the scene's funscript JSON for the `FunscriptPlayer`.
- **SOCKS5** — when enabled, all requests tunnel through a SOCKS5 proxy implemented with the stdlib `socket` module (no extra dependency).

During a session, a `play_video` turn (chosen by the model, or injected at rate
`video_chance`) resolves to a random scene; the UI plays the proxied video while its
funscript drives the device, and `POST /api/video/ended` releases the display hold.

### Funscript player

`FunscriptPlayer` parses a funscript (single- or multi-axis), sorts its actions, and
schedules each one with a `threading.Timer`, sending `stream` position commands to the
active device. It supports `start`/`pause`/`resume`/`seek`/`stop`, reports live progress
and the interpolated current position, and has runtime `latency_ms` and `invert` tuning.

### Text-to-speech

`tts.py` wraps [Kokoro](https://github.com/hexgrad/kokoro). `synthesize()` returns an
audio URL plus **word-level timings** (start/end ms per word) so the UI can highlight
each word as it plays, and caches results on disk keyed by text+voice+speed. Voice,
speed, and device (`auto`/`cpu`/`cuda`) come from settings via `tts.configure()`; Kokoro
and `soundfile` are imported lazily so the app starts even when TTS isn't installed.

---

## Settings

Runtime configuration lives in `~/.config/aimee/settings.json`, managed by
`settings_store.py`. `config.py` provides the defaults (each overridable by an
environment variable); saved settings then override those, and values are
**range-clamped and normalized** on save.

The **Settings tab** edits everything through one **global Save All** button with an
unsaved-changes indicator. Service cards (Google, Groq, Stash) additionally have a
**Test Connection** button that validates just that service and updates the status panel
on the right — saving stays fast and never blocks on the network, while tests own
connectivity checks. Changing a credential resets that service's stored validation to
"pending".

Settings categories: **Google AI**, **Groq AI**, **Generation** (temperature, top-p,
top-k, timeouts, retries), **Session & Pacing** (turns, watermarks,
display interval, generator sleep, banned-phrase window), **Text-to-Speech** (enable,
voice, speed, device), **Stash** (enable + interlude chance, URL, key, tag, SOCKS5),
**Device** (default WS URL, Coyote BLE name + soft limits + frequency), and **Prompt
Files**.

---

## Prompt storage

Prompts live under `prompts/` in two layers:

- `prompts/base/` — immutable source files (the system prompt, seeds, examples, tasks, traits, wordlist).
- `prompts/current/` — editable overrides (created the first time one is saved).
- `prompts/catalog/` — additional packaged bundles (read-only).

When a prompt is read, `prompt_store.py` checks `current/` first and falls back to
`base/`. Uploading writes to `current/` under the same relative filename; **only names
that already exist in `base/` are accepted**. The Settings tab can download the resolved
file, upload an override, or **Revert** (delete all `current/` overrides) to return to
the base versions. Editing prompts reloads them live via `orchestrator.reload_prompts()`.

---

## Authoring intents and patterns

**Add a motion pattern:** drop a `patterns/<name>.json` describing the pattern (it gets
serialized into the system prompt). If the device firmware needs a slot for it, add the
name to `AI_TO_DEVICE_PATTERN_MAP` in `config.py`.

**Add an intent:** create `intents/<intent>/` and one JSON file per intensity band. A
band looks like:

```json
{
  "name": "tease_hard",
  "description": "Harder, but still teasing.",
  "intensity_range": [0.7, 1.0],
  "pattern": "simple_stroke",
  "speed": 20,
  "depth": 30,
  "base": 0,
  "duration_ms": 6000,
  "easing": "sine_in_out",
  "variations": [
    { "probability": 0.5, "pattern": "half_n_half", "speed": 10, "depth": 40 },
    { "probability": 0.3, "pattern": "stop_n_go",  "speed": 30, "depth": 20, "intensity": 50 },
    { "probability": 0.2, "speed": 5, "depth": 40 }
  ]
}
```

`intensity_range` is required; `variations` are optional and chosen by weighted
probability and merged over the band's base parameters. Intents reload live (`reload()`),
and the model only emits intents that have band files, so keep the prompt's intent list
in sync with what's on disk.

---

## HTTP API

Selected endpoints (see `routes.py` for the full set):

**Session**
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/start` | Start a session (`n_turns`, `persona`, `pacing`, `model`). |
| `POST` | `/api/pause` · `/api/resume` · `/api/clear` | Session controls. |
| `GET`  | `/api/poll?since=<n>` | Fetch newly displayed turns. |
| `POST` | `/api/feedback` | React to a displayed turn (`index`, `reaction` = `like`/`love`/`dislike`/`clear`/`ban`/`unban`). |
| `POST` | `/api/video/ended` | Signal the on-screen clip finished. |
| `GET`  | `/api/health` | Big-model health + orchestrator status. |

**Settings & prompts**
| Method | Path | Purpose |
|--------|------|---------|
| `GET` / `POST` | `/api/settings` | Read all settings / save all settings. |
| `POST` | `/api/settings/test/<google\|groq\|stash>` | Validate one service. |
| `GET` / `POST` | `/api/prompts/<name>` | Download / upload an override. |
| `POST` | `/api/prompts/revert` | Delete all overrides. |
| `GET`  | `/api/intents` | List intents and intensity coverage. |

**Devices**
| Method | Path | Purpose |
|--------|------|---------|
| `GET` / `POST` | `/api/device/types` · `/api/device/set` | List / switch device type. |
| `POST` | `/api/device/connect` · `/api/device/disconnect` · `/api/device/home` | Connection control. |
| `GET`  | `/api/device/state` · `/api/device/stream` | State snapshot / live SSE stream. |
| `POST` | `/api/device/command` | Send a raw command dict. |
| `POST` | `/api/device/serial_emulator/start` · `/stop` | Local PTY + emulator. |
| `GET` / `POST` | `/api/coyote/scan` · `/api/coyote/command` | Coyote BLE scan / command. |

**Media**
| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/api/stash/scenes` | List tagged scenes (`?refresh=1` to force). |
| `POST` | `/api/stash/funscript/<id>` | Load a scene's funscript into the player. |
| `GET`  | `/api/stash/video/<id>` | Proxy a scene's video stream (Range-aware). |
| `POST` | `/api/funscript/upload` · `/play` · `/load` · `/start` · `/pause` · `/resume` · `/seek` · `/stop` | Funscript playback. |
| `GET`  | `/api/funscript/status` · `/list` · `/videos` | Player status / file lists. |
| `POST` | `/api/tts/synthesize` · `/clear` | TTS synth / cache clear. |
| `GET`  | `/api/tts/audio/<key>` | Fetch cached audio. |

---

## Project structure

```text
AIMO/
├── main.py                 # Entry point
├── app_factory.py          # Flask app factory (logging, routes, default device)
├── routes.py               # All HTTP routes; owns the orchestrator + funscript player
├── config.py               # Central defaults (env-overridable); pattern→slot map
├── settings_store.py       # Local settings load / normalize / save
├── orchestrator.py         # Producer/consumer loops + session lifecycle
├── ai_connector.py         # Google & Groq stateful chat connectors
├── brain.py                # Session/creative coordinator
├── prompt_builder.py       # System / seed / per-turn prompt construction
├── prompt_store.py         # base/current prompt resolution
├── response_parser.py      # Raw model text → Turn objects
├── intent_compiler.py      # (intent, intensity) → CompiledCommand
├── pattern_loader.py       # Loads patterns/*.json
├── session_manager.py      # In-memory turns + effective device state
├── feedback_store.py       # Persistent banned-phrase store (cross-session feedback)
├── stash_client.py         # Stash GraphQL/media client (+ stdlib SOCKS5)
├── funscript_player.py     # Funscript scheduling/playback
├── tts.py                  # Kokoro TTS with word timing + cache
├── device_bridge.py        # Legacy accessor → registry singleton
├── device_emulator.py      # Standalone OSSM firmware emulator
├── heart_rate_sensor.py    # Standalone BLE heart-rate experiment (not wired into the app)
├── devices/
│   ├── base.py             # AbstractDevice + DeviceState
│   ├── ossm.py             # OSSM (WebSocket / serial)
│   ├── coyote_ble.py       # Coyote 3.0 (BLE)
│   └── registry.py         # Active-device factory/singleton
├── intents/                # <intent>/<intensity-band>.json
├── patterns/               # Motion patterns + custom/ and funscripts/ + videos/
├── prompts/{base,current,catalog}/
├── templates/              # index.html + tab_*.html partials
├── static/{css,js}/        # UI styles and per-tab scripts
└── logs/                   # API response logs
```

---

## Configuration reference

Every value below is an environment-variable default in `config.py`; most are also
editable at runtime from the Settings tab (which persists to
`~/.config/aimee/settings.json`).

| Variable | Description | Default |
|----------|-------------|---------|
| `GOOGLE_MODEL` / `GROQ_MODEL` | Default model per provider | `gemma-4-31b-it` / `openai/gpt-oss-120b` |
| `GEN_TEMPERATURE` / `GEN_TOP_P` / `GEN_TOP_K` | Generation sampling | `1.2` / `0.90` / `60` |
| `GOOGLE_TIMEOUT` / `GROQ_TIMEOUT` | API timeouts (s) | `240` / `240` |
| `BIG_MAX_RETRIES` / `BIG_RETRY_DELAY` | Big-model retry policy | `3` / `30` |
| `DEFAULT_TURNS` | Default session length | `5` |
| `BANNED_PHRASE_WINDOW` | Recent lines fed back as "do not repeat" | `20` |
| `VIDEO_CHANCE` | Per-turn chance of a video interlude | `0.10` |
| `KOKORO_VOICE` / `KOKORO_SPEED` / `KOKORO_DEVICE` | TTS voice / speed / device | `af_heart` / `1.0` / `auto` |
| `STASH_URL` / `STASH_API_KEY` / `STASH_TAG` | Stash server, key, playable tag | `""` / `""` / `playable` |
| `STASH_VIDEO_ENABLED` | Allow video interludes | `true` |
| `STASH_PROXY_ENABLED` / `STASH_PROXY_ADDRESS` | SOCKS5 tunnel | `false` / `""` |
| `DEVICE_WS_URL` | Default OSSM WebSocket URL | `ws://localhost:8888` |
| `COYOTE_BLE_NAME` | Coyote BLE advertised name | `47L121000` |
| `COYOTE_SOFT_LIMIT_A` / `_B` | Channel strength caps | `100` / `100` |
| `COYOTE_DEFAULT_FREQ_MS` | Coyote pulse frequency (ms) | `100` |
| `FLASK_HOST` / `FLASK_PORT` / `FLASK_DEBUG` | Server bind/port/debug | `0.0.0.0` / `5000` / `true` |
| `LOG_LEVEL` | Root log level | `INFO` |

Watermark/timing internals (`DISPLAY_INTERVAL`, `LOW_WATERMARK`, `HIGH_WATERMARK`,
`GENERATOR_SLEEP`) are settable from the Settings tab as well.

API keys are never committed to source — they live only in the local settings file.

---

## Dependencies

`requirements.txt` (and the prebuilt binaries) include:

- **Core** — `Flask`, `google-genai`, `websockets`, `pyserial`.
- **Local TTS (Kokoro)** — `kokoro`, `misaki[en]`, `soundfile`, `numpy`.
- **Coyote BLE** — `bleak`.

All of these are still **lazy-loaded**: the app starts and runs even if an optional
package is missing, and a feature that needs one surfaces a clear error only when first
used. A few extras are not bundled and remain install-as-needed:

- **Groq** — none beyond the stdlib (uses `urllib`).
- **Heart-rate sensor** — `pynput` (for the standalone heart-rate experiment).
- **Serial emulator** — the `socat` system package (Linux/macOS; used by the one-click
  PTY emulator on the Setup tab).
