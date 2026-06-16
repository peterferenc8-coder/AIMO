"""
routes.py
---------
All Flask HTTP routes for the OSSM Controller + Coyote BLE.
"""

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import atexit
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file

from ai_connector import GoogleAIConnector, GroqAIConnector
from config import AI_TO_DEVICE_PATTERN_MAP, GROQ_MODEL_OPTIONS, MODEL_OPTIONS
from devices.registry import (
    get_active_device,
    get_active_type,
    list_device_types,
    set_active_device,
)
from orchestrator import SessionOrchestrator
from prompt_store import (
    clear_current_prompts,
    list_base_prompt_names,
    prompt_exists_in_base,
    resolve_prompt_path,
    write_current_prompt,
)
from prompt_builder import get_pacing_strategies, get_persona_moods
from settings_store import load_settings, mask_secret, provider_presence, save_settings
import tts

from config import PATTERNS_DIR

CUSTOM_PATTERNS_DIR = PATTERNS_DIR / "custom"
CUSTOM_PATTERNS_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger(__name__)

_orchestrator = SessionOrchestrator()


class _SerialEmulatorLauncher:
    """Starts/stops a local PTY bridge and emulator process for setup testing."""

    def __init__(self):
        self._lock = threading.Lock()
        self._socat_proc: subprocess.Popen[str] | None = None
        self._emu_proc: subprocess.Popen[str] | None = None
        self.device_port: str | None = None
        self.controller_port: str | None = None
        self.device_link = "/tmp/aimee_pty_device"
        self.controller_link = "/tmp/aimee_pty_app"
        atexit.register(self.stop)

    def start(self) -> dict[str, str | bool]:
        with self._lock:
            if self._is_running():
                return {
                    "ok": True,
                    "already_running": True,
                    "device_port": self.device_port or "",
                    "controller_port": self.controller_port or "",
                }

            self._stop_locked()

            if shutil.which("socat") is None:
                return {
                    "ok": False,
                    "error": "socat is not installed. Install it first: sudo apt install socat",
                }

            self._cleanup_pty_links()

            try:
                self._socat_proc = subprocess.Popen(
                    [
                        "socat",
                        f"pty,raw,echo=0,link={self.device_link},mode=666",
                        f"pty,raw,echo=0,link={self.controller_link},mode=666",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
            except Exception as exc:
                self._stop_locked()
                return {"ok": False, "error": f"Failed to start socat: {exc}"}

            if not self._wait_for_pty_links(timeout_sec=3.0):
                self._stop_locked()
                return {
                    "ok": False,
                    "error": "Could not create PTY links under /tmp.",
                }

            self.device_port, self.controller_port = self.device_link, self.controller_link

            emulator_path = Path(__file__).resolve().parent / "device_emulator.py"
            try:
                self._emu_proc = subprocess.Popen(
                    [sys.executable, str(emulator_path), "--serial", self.device_port],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
            except Exception as exc:
                self._stop_locked()
                return {"ok": False, "error": f"Failed to start emulator: {exc}"}

            time.sleep(0.2)
            if self._emu_proc.poll() is not None:
                self._stop_locked()
                return {
                    "ok": False,
                    "error": "device_emulator.py exited immediately after launch.",
                }

            return {
                "ok": True,
                "already_running": False,
                "device_port": self.device_port,
                "controller_port": self.controller_port,
            }

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        for proc in (self._emu_proc, self._socat_proc):
            if not proc:
                continue
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        self._emu_proc = None
        self._socat_proc = None
        self.device_port = None
        self.controller_port = None
        self._cleanup_pty_links()

    def _is_running(self) -> bool:
        return (
            self._socat_proc is not None
            and self._socat_proc.poll() is None
            and self._emu_proc is not None
            and self._emu_proc.poll() is None
            and bool(self.device_port)
            and bool(self.controller_port)
        )

    def _wait_for_pty_links(self, timeout_sec: float) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if self._socat_proc is None or self._socat_proc.poll() is not None:
                return False
            if os.path.exists(self.device_link) and os.path.exists(self.controller_link):
                return True
            time.sleep(0.05)
        return False

    def _cleanup_pty_links(self) -> None:
        for path in (self.device_link, self.controller_link):
            try:
                if os.path.islink(path) or os.path.exists(path):
                    os.unlink(path)
            except Exception:
                pass


_serial_emulator = _SerialEmulatorLauncher()

def _funscript_send(cmd: dict):
    dev = get_active_device()
    if dev and dev.get_state().connected:
        dev.send_command(cmd)

from funscript_player import FunscriptPlayer

_funscript_player = FunscriptPlayer(send_command_fn=_funscript_send)

def _keep_existing(value: str | None, fallback: str) -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def _validation_from_settings(settings: dict, key: str) -> dict:
    value = settings.get(key)
    if isinstance(value, dict):
        return value
    return {
        "ok": False,
        "message": "Not validated yet",
        "checked_at": None,
    }


def _saved_settings_payload(settings: dict) -> dict:
    return {
        "google_api_key_masked": mask_secret(settings.get("google_api_key", "")),
        "groq_api_key_masked": mask_secret(settings.get("groq_api_key", "")),
        "google_key_present": bool(settings.get("google_api_key", "")),
        "groq_key_present": bool(settings.get("groq_api_key", "")),
        "google_model": settings.get("google_model", ""),
        "groq_model": settings.get("groq_model", ""),
        "stash_url": settings.get("stash_url", ""),
        "stash_api_key_masked": mask_secret(settings.get("stash_api_key", "")),
        "stash_key_present": bool(str(settings.get("stash_api_key", "") or "").strip()),
        "stash_tag": settings.get("stash_tag", ""),
    }


def _available_ai_models(settings: dict) -> list[str]:
    models: list[str] = []

    google_valid = bool(settings.get("google_validation", {}).get("ok"))
    google_present = bool(str(settings.get("google_api_key", "") or "").strip())
    if google_present and google_valid:
        models.extend(MODEL_OPTIONS)

    groq_valid = bool(settings.get("groq_validation", {}).get("ok"))
    groq_present = bool(str(settings.get("groq_api_key", "") or "").strip())
    if groq_present and groq_valid:
        models.extend(GROQ_MODEL_OPTIONS)

    return models


def register_routes(app: Flask) -> None:
    """Attach all routes to the provided Flask application."""

    @app.get("/")
    def index():
        """Serve the main GUI."""
        settings = load_settings()
        model_options = _available_ai_models(settings)

        selected_model = settings.get("google_model", _orchestrator.big_connector.model)
        if selected_model not in model_options:
            selected_model = model_options[0] if model_options else ""

        patterns = [
            {"name": name.replace("_", " ").title(), "index": idx}
            for name, idx in AI_TO_DEVICE_PATTERN_MAP.items()
            if name != "stop"
        ]
        patterns.sort(key=lambda p: p["index"])

        return render_template(
            "index.html",
            persona_moods=get_persona_moods(),
            pacing_strategies=get_pacing_strategies(),
            model_options=model_options,
            selected_model=selected_model,
            settings=settings,
            patterns=patterns,
        )

    # ── Device Type Management ──────────────────────────────────────────────

    @app.get("/api/device/types")
    def api_device_types():
        return jsonify({
            "ok": True,
            "types": list_device_types(),
            "active": get_active_type(),
        })

    @app.post("/api/device/set")
    def api_device_set():
        body = request.get_json(silent=True) or {}
        device_type = body.get("type", "ossm")
        try:
            dev = set_active_device(device_type)
            return jsonify({"ok": True, "type": device_type, "name": dev.name})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    # ── Settings ──────────────────────────────────────────────────────────────

    @app.get("/api/settings")
    def api_settings():
        settings = load_settings()
        presence = provider_presence(settings)
        google_validation = _validation_from_settings(settings, "google_validation")
        groq_validation = _validation_from_settings(settings, "groq_validation")

        return jsonify(
            {
                "ok": True,
                "google_api_key_masked": mask_secret(settings.get("google_api_key", "")),
                "groq_api_key_masked": mask_secret(settings.get("groq_api_key", "")),
                "google_key_present": presence["google"],
                "groq_key_present": presence["groq"],
                "google_model": settings.get("google_model", ""),
                "groq_model": settings.get("groq_model", ""),
                "google_model_options": MODEL_OPTIONS,
                "groq_model_options": GROQ_MODEL_OPTIONS,
                "google_validation": google_validation,
                "groq_validation": groq_validation,
                "tts_enabled": settings.get("tts_enabled", True),
                "stash_url": settings.get("stash_url", ""),
                "stash_api_key_masked": mask_secret(settings.get("stash_api_key", "")),
                "stash_key_present": bool(str(settings.get("stash_api_key", "") or "").strip()),
                "stash_tag": settings.get("stash_tag", ""),
                "stash_validation": _validation_from_settings(settings, "stash_validation"),
                "prompt_names": list_base_prompt_names(),
            }
        )

    @app.post("/api/settings")
    def api_settings_save():
        body = request.get_json(silent=True) or {}
        current = load_settings()

        next_settings = {
            "google_api_key": _keep_existing(body.get("google_api_key"), current.get("google_api_key", "")),
            "groq_api_key": _keep_existing(body.get("groq_api_key"), current.get("groq_api_key", "")),
            "google_model": _keep_existing(body.get("google_model"), current.get("google_model", "")),
            "groq_model": _keep_existing(body.get("groq_model"), current.get("groq_model", "")),
            "tts_enabled": body.get("tts_enabled", current.get("tts_enabled", True)),
            "stash_url": (str(body["stash_url"]).strip() if "stash_url" in body
                          else current.get("stash_url", "")).rstrip("/"),
            "stash_api_key": _keep_existing(body.get("stash_api_key"), current.get("stash_api_key", "")),
            "stash_tag": (str(body["stash_tag"]).strip() if "stash_tag" in body
                          else current.get("stash_tag", "")),
        }

        google_validation = _validate_google_key(next_settings["google_api_key"], next_settings["google_model"])
        groq_validation = _validate_groq_key(next_settings["groq_api_key"], next_settings["groq_model"])

        next_settings["google_validation"] = google_validation
        next_settings["groq_validation"] = groq_validation

        save_settings(next_settings)
        _orchestrator.apply_settings(next_settings)

        # Validate Stash against the freshly-applied client (network round-trip).
        stash_validation = _orchestrator.stash.validate()
        next_settings["stash_validation"] = stash_validation
        save_settings(next_settings)

        return jsonify(
            {
                "ok": True,
                "saved": _saved_settings_payload(next_settings),
                "google_validation": google_validation,
                "groq_validation": groq_validation,
                "stash_validation": stash_validation,
                "tts_enabled": next_settings["tts_enabled"],
                "prompt_names": list_base_prompt_names(),
            }
        )

    @app.get("/api/prompts/<path:prompt_name>")
    def api_prompt_download(prompt_name: str):
        if not prompt_exists_in_base(prompt_name):
            return jsonify({"ok": False, "error": "Unknown prompt file"}), 404

        prompt_path = resolve_prompt_path(prompt_name)
        if not prompt_path.exists():
            return jsonify({"ok": False, "error": "Prompt file not found"}), 404

        return send_file(
            prompt_path,
            as_attachment=True,
            download_name=Path(prompt_name).name,
            mimetype="text/plain",
        )

    @app.post("/api/prompts/<path:prompt_name>")
    def api_prompt_upload(prompt_name: str):
        if not prompt_exists_in_base(prompt_name):
            return jsonify({"ok": False, "error": "Unknown prompt file"}), 400

        upload = request.files.get("file")
        if upload is None:
            return jsonify({"ok": False, "error": "Missing uploaded file"}), 400

        uploaded_name = Path(upload.filename or "").name
        expected_name = Path(prompt_name).name
        if uploaded_name != expected_name:
            return jsonify({"ok": False, "error": f"Expected file named {expected_name}"}), 400

        content = upload.read().decode("utf-8")
        destination = write_current_prompt(prompt_name, content)
        _orchestrator.reload_prompts()

        return jsonify(
            {
                "ok": True,
                "name": prompt_name,
                "written_to": str(destination),
            }
        )

    @app.get("/api/intents")
    def api_intents():
        """List all available intents and their metadata."""
        return jsonify({
            "ok": True,
            "intents": [
                _orchestrator.intent_compiler.get_intent_meta(name)
                for name in _orchestrator.intent_compiler.list_intents()
            ]
        })

    @app.post("/api/prompts/revert")
    def api_prompts_revert():
        removed = clear_current_prompts()
        _orchestrator.reload_prompts()
        return jsonify({"ok": True, "removed": removed})

    # ── Session ───────────────────────────────────────────────────────────────

    @app.post("/api/start")
    def api_start():
        body = request.get_json(silent=True) or {}
        n_turns = int(body.get("n_turns", 20))
        persona = body.get("persona")
        pacing = body.get("pacing")
        model = body.get("model")

        allowed_models = _available_ai_models(load_settings())

        if model not in allowed_models:
            model = None

        status = _orchestrator.start(
            n_turns=n_turns,
            persona=persona,
            pacing=pacing,
            model=model,
        )
        return jsonify(status)

    @app.post("/api/pause")
    def api_pause():
        return jsonify(_orchestrator.pause())

    @app.post("/api/resume")
    def api_resume():
        return jsonify(_orchestrator.resume())

    @app.post("/api/clear")
    def api_clear():
        return jsonify(_orchestrator.clear())

    @app.get("/api/poll")
    def api_poll():
        since = request.args.get("since", 0, type=int)
        return jsonify(_orchestrator.poll(since_index=since))

    @app.get("/api/health")
    def api_health():
        status = _orchestrator.big_connector.health_check()
        code = 200 if status["ok"] else 503
        status.update(_orchestrator.status)
        return jsonify(status), code

    # ── Generic Device Routes ───────────────────────────────────────────────

    @app.post("/api/device/connect")
    def api_device_connect():
        body = request.get_json(silent=True) or {}
        url = body.get("url", "ws://localhost:8888")
        dev = get_active_device()
        if not dev:
            return jsonify({"ok": False, "error": "No device selected"}), 400
        ok = dev.connect(url)
        return jsonify({"ok": ok, "url": url, "state": dev.latest_state})

    @app.post("/api/device/disconnect")
    def api_device_disconnect():
        dev = get_active_device()
        if dev:
            dev.disconnect()
        return jsonify({"ok": True})

    @app.post("/api/device/home")
    def api_device_home():
        """Home the device by sending setZero (OSSM only)."""
        dev = get_active_device()
        if not dev:
            return jsonify({"ok": False, "error": "No device"}), 400
        if dev.device_type == "ossm":
            dev.send_command({"cmd": "setZero"})
        return jsonify({"ok": True})

    @app.get("/api/device/state")
    def api_device_state():
        dev = get_active_device()
        if not dev:
            return jsonify({"ok": False, "error": "No device"}), 400
        return jsonify({"ok": True, **dev.latest_state})

    @app.get("/api/device/stream")
    def api_device_stream():
        def generate():
            q = queue.Queue(maxsize=30)

            def on_data(data):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass

            dev = get_active_device()
            if not dev:
                yield f"data: {json.dumps({'type': 'error', 'message': 'No device'})}\n\n"
                return

            dev.add_listener(on_data)
            try:
                yield f"data: {json.dumps({'type': 'position', **dev.latest_state})}\n\n"
                while True:
                    try:
                        data = q.get(timeout=2)
                        # Ensure type: position is present for app.js compatibility
                        payload = {'type': 'position', **data}
                        yield f"data: {json.dumps(payload)}\n\n"
                    except queue.Empty:
                        yield f"data: {json.dumps({'type': 'ping'})}\n\n"
            finally:
                dev.remove_listener(on_data)

        response = Response(generate(), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @app.post("/api/device/command")
    def api_device_command():
        """Send a raw command dict to the active device."""
        body = request.get_json(silent=True) or {}
        if "cmd" not in body:
            return jsonify({"ok": False, "error": "Missing cmd"}), 400

        dev = get_active_device()
        if not dev:
            return jsonify({"ok": False, "error": "No device"}), 400

        if not dev.get_state().connected:
            return jsonify({"ok": False, "error": "Device is not connected"}), 409

        dev.send_command(body)
        return jsonify({"ok": True})

    @app.post("/api/device/serial_emulator/start")
    def api_device_serial_emulator_start():
        """Start a local Linux PTY pair and attach device_emulator.py to one side."""
        result = _serial_emulator.start()
        status = 200 if result.get("ok") else 500
        return jsonify(result), status

    @app.post("/api/device/serial_emulator/stop")
    def api_device_serial_emulator_stop():
        _serial_emulator.stop()
        return jsonify({"ok": True})

    # ── Coyote-Specific Routes ──────────────────────────────────────────────

    @app.get("/api/coyote/scan")
    def api_coyote_scan():
        from devices.coyote_ble import CoyoteBLE
        try:
            results = CoyoteBLE.scan(timeout=5.0)
            return jsonify({"ok": True, "devices": results})
        except Exception as exc:
            log.error("BLE scan error: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.post("/api/coyote/command")
    def api_coyote_command():
        body = request.get_json(silent=True) or {}
        dev = get_active_device()
        if not dev or dev.device_type != "coyote":
            return jsonify({"ok": False, "error": "Coyote not active"}), 400

        if not dev.get_state().connected:
            return jsonify({"ok": False, "error": "Coyote not connected"}), 409

        dev.send_command(body)
        return jsonify({"ok": True, "state": dev.latest_state})

    # ── TTS Routes ──────────────────────────────────────────────────────────

    @app.get("/api/tts/audio/<string:cache_key>")
    def api_tts_audio(cache_key: str):
        path = tts.get_audio_path(cache_key)
        if path is None or not path.exists():
            abort(404)
        return send_file(path, mimetype="audio/wav")

    @app.get("/api/tts/cache")
    def api_tts_cache():
        return jsonify({"ok": True, "items": tts.list_cache()})

    @app.post("/api/tts/clear")
    def api_tts_clear():
        count = tts.clear_cache()
        return jsonify({"ok": True, "removed": count})

    @app.post("/api/tts/synthesize")
    def api_tts_synthesize():
        body = request.get_json(silent=True) or {}
        text = body.get("text", "")
        if not text or not text.strip():
            return jsonify({"ok": False, "error": "Missing text"}), 400

        try:
            result = tts.synthesize(
                text,
                voice=body.get("voice"),
                speed=body.get("speed"),
            )
            return jsonify({"ok": True, **result})
        except Exception as exc:
            log.error("TTS synthesis error: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 500

    # ── Custom patterns ─────────────────────────────────────────────────────

    @app.get("/api/custom_patterns")
    def api_list_custom_patterns():
        patterns = [p.stem for p in CUSTOM_PATTERNS_DIR.glob("*.json")]
        return jsonify({"ok": True, "patterns": sorted(patterns)})

    @app.get("/api/custom_patterns/<name>")
    def api_get_custom_pattern(name):
        p = CUSTOM_PATTERNS_DIR / f"{name}.json"
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return jsonify({"ok": True, "points": json.load(f)})
        return jsonify({"ok": False, "error": "Not found"}), 404

    @app.post("/api/custom_patterns/<name>")
    def api_save_custom_pattern(name):
        body = request.get_json(silent=True) or {}
        points = body.get("points", [])
        p = CUSTOM_PATTERNS_DIR / f"{name}.json"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(points, f, indent=2)
        return jsonify({"ok": True})

    # ── Funscript ─────────────────────────────────────────────────────────────

    FUNSCRIPT_DIR = PATTERNS_DIR / "funscripts"
    FUNSCRIPT_DIR.mkdir(parents=True, exist_ok=True)

    @app.post("/api/funscript/upload")
    def api_funscript_upload():
        """Receive raw funscript JSON, save to disk, and load into player."""
        body = request.get_json(silent=True) or {}
        data = body.get("data")
        filename = body.get("filename", "uploaded.funscript")

        if not data or not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Missing or invalid 'data' field"}), 400
        if not data.get("actions"):
            return jsonify({"ok": False, "error": "No actions found in funscript"}), 400

        safe_name = os.path.basename(filename)
        if not safe_name.endswith(".funscript"):
            safe_name += ".funscript"

        filepath = FUNSCRIPT_DIR / safe_name
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            meta = _funscript_player.load_file(str(filepath))
            return jsonify({
                "ok": True,
                "filepath": str(filepath),
                **meta
            })
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.post("/api/funscript/play")
    def api_funscript_play():
        """
        3rd-party endpoint: receive raw funscript JSON and immediately start playing.
        Body: {"data": {...}, "offset_ms": 0}
        """
        body = request.get_json(silent=True) or {}
        data = body.get("data")
        offset_ms = int(body.get("offset_ms", 0))

        if not data or not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Missing or invalid 'data' field"}), 400
        if not data.get("actions"):
            return jsonify({"ok": False, "error": "No actions found in funscript"}), 400

        dev = get_active_device()
        if not dev:
            return jsonify({"ok": False, "error": "No device connected"}), 400
        if not dev.get_state().connected:
            return jsonify({"ok": False, "error": "Device is not connected"}), 409

        try:
            meta = _funscript_player.load_data(data)
            _funscript_player.start(offset_ms)
            return jsonify({
                "ok": True,
                "status": "playing",
                **meta
            })
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.post("/api/funscript/load")
    def api_funscript_load():
        """Load a saved funscript by filename or full path."""
        body = request.get_json(silent=True) or {}
        filepath = body.get("filepath")
        filename = body.get("filename")

        if not filepath and filename:
            filepath = str(FUNSCRIPT_DIR / os.path.basename(filename))
            if not filepath.endswith(".funscript"):
                filepath += ".funscript"

        if not filepath or not os.path.exists(filepath):
            return jsonify({"ok": False, "error": "File not found"}), 404

        try:
            meta = _funscript_player.load_file(filepath)
            with open(filepath, "r", encoding="utf-8") as f:
                file_data = json.load(f)
            return jsonify({
                "ok": True,
                "filepath": filepath,
                "data": file_data,
                **meta
            })
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/funscript/download/<name>")
    def api_funscript_download(name):
        """Return raw funscript JSON for a saved file."""
        p = FUNSCRIPT_DIR / name
        if not p.exists() or not p.name.endswith(".funscript"):
            p = FUNSCRIPT_DIR / f"{name}.funscript"
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return jsonify({"ok": True, "data": json.load(f)})
        return jsonify({"ok": False, "error": "Not found"}), 404

    @app.post("/api/funscript/start")
    def api_funscript_start():
        body = request.get_json(silent=True) or {}
        offset_ms = int(body.get("offset_ms", 0))

        dev = get_active_device()
        if not dev:
            return jsonify({"ok": False, "error": "No device connected"}), 400
        if not dev.get_state().connected:
            return jsonify({"ok": False, "error": "Device is not connected"}), 409

        ok = _funscript_player.start(offset_ms)
        return jsonify({"ok": ok, "status": "started" if ok else "empty", "offset_ms": offset_ms})

    @app.post("/api/funscript/pause")
    def api_funscript_pause():
        _funscript_player.pause()
        return jsonify({"ok": True, "status": "paused"})

    @app.post("/api/funscript/resume")
    def api_funscript_resume():
        _funscript_player.resume()
        return jsonify({"ok": True, "status": "resumed"})

    @app.post("/api/funscript/seek")
    def api_funscript_seek():
        body = request.get_json(silent=True) or {}
        pos = int(body.get("position_ms", 0))
        _funscript_player.seek(pos)
        return jsonify({"ok": True, "status": "seeked", "position_ms": pos})

    @app.post("/api/funscript/stop")
    def api_funscript_stop():
        _funscript_player.stop()
        return jsonify({"ok": True, "status": "stopped"})

    @app.get("/api/funscript/status")
    def api_funscript_status():
        return jsonify({"ok": True, **_funscript_player.get_status()})

    @app.get("/api/funscript/list")
    def api_funscript_list():
        patterns = [p.name for p in FUNSCRIPT_DIR.glob("*.funscript")]
        return jsonify({"ok": True, "files": sorted(patterns)})
    
    @app.route('/api/funscript/config', methods=['POST'])
    def funscript_config():
        """Set or get FunscriptPlayer runtime config (latency & invert)."""
        data = request.get_json() or {}

        # Adjust this line to match however you access your FunscriptPlayer instance
        # e.g. player = g.funscript_player  or  player = session_manager.funscript
        player = _funscript_player  # <-- CHANGE THIS to your actual instance reference

        if 'latency_ms' in data or 'invert' in data:
            player.set_config(
                latency_ms=data.get('latency_ms'),
                invert=data.get('invert')
            )
        return jsonify(player.get_config())
    
    # ── Funscript Videos ─────────────────────────────────────────────────────

    VIDEOS_DIR = PATTERNS_DIR / "videos"
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    @app.post("/api/funscript/video/upload")
    def api_funscript_video_upload():
        upload = request.files.get("file")
        if not upload:
            return jsonify({"ok": False, "error": "Missing file"}), 400
        safe_name = os.path.basename(upload.filename or "video.mp4")
        filepath = VIDEOS_DIR / safe_name
        upload.save(filepath)
        return jsonify({"ok": True, "filepath": str(filepath), "filename": safe_name})

    @app.get("/api/funscript/videos")
    def api_funscript_videos():
        exts = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
        files = [f.name for f in VIDEOS_DIR.iterdir() if f.suffix.lower() in exts]
        return jsonify({"ok": True, "files": sorted(files)})

    @app.get("/api/funscript/video/<name>")
    def api_funscript_video_download(name):
        p = VIDEOS_DIR / os.path.basename(name)
        if p.exists():
            return send_file(p)
        return jsonify({"ok": False, "error": "Not found"}), 404

    # ── Stash integration ──────────────────────────────────────────────────────

    @app.get("/api/stash/scenes")
    def api_stash_scenes():
        """List (and optionally refresh) the tagged, playable Stash scenes."""
        stash = _orchestrator.stash
        if not stash.is_configured():
            return jsonify({"ok": False, "error": "Stash is not configured"}), 400
        force = request.args.get("refresh") == "1"
        try:
            scenes = stash.get_scenes(force=force)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502
        return jsonify({"ok": True, "count": len(scenes), "scenes": scenes})

    @app.post("/api/stash/funscript/<scene_id>")
    def api_stash_funscript(scene_id):
        """Fetch a scene's funscript from Stash and load it into the player."""
        stash = _orchestrator.stash
        if not stash.is_configured():
            return jsonify({"ok": False, "error": "Stash is not configured"}), 400
        try:
            data = stash.fetch_funscript(scene_id)
            meta = _funscript_player.load_data(data)
            return jsonify({"ok": True, "scene_id": scene_id, **meta})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.get("/api/stash/video/<scene_id>")
    def api_stash_video(scene_id):
        """Proxy a scene's video stream from Stash (keeps the API key server-side)."""
        stash = _orchestrator.stash
        if not stash.is_configured():
            return jsonify({"ok": False, "error": "Stash is not configured"}), 400

        upstream = stash.open_stream(scene_id, request.headers.get("Range"))
        status = getattr(upstream, "status", None) or getattr(upstream, "code", 200)

        if status >= 400:
            try:
                upstream.close()
            except Exception:
                pass
            return jsonify({"ok": False, "error": f"Stash returned {status}"}), 502

        passthrough = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")
        resp_headers = {}
        for header in passthrough:
            value = upstream.headers.get(header)
            if value is not None:
                resp_headers[header] = value
        resp_headers.setdefault("Accept-Ranges", "bytes")

        def generate():
            try:
                while True:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass

        return Response(generate(), status=status, headers=resp_headers)



def _validate_google_key(api_key: str, model: str) -> dict:
    connector = GoogleAIConnector(api_key=api_key, model=model)
    return connector.validate_api_key()


def _validate_groq_key(api_key: str, model: str) -> dict:
    connector = GroqAIConnector(api_key=api_key, model=model)
    return connector.validate_api_key()

