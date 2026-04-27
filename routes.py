"""
routes.py
---------
All Flask HTTP routes for the dual-model OSSM Controller.
"""

import json
import logging
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file

from ai_connector import GoogleAIConnector, GroqAIConnector
from config import AI_TO_DEVICE_PATTERN_MAP, GROQ_MODEL_OPTIONS, MODEL_OPTIONS
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

import queue
from device_bridge import get_bridge

log = logging.getLogger(__name__)

_orchestrator = SessionOrchestrator()


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
        }

        google_validation = _validate_google_key(next_settings["google_api_key"], next_settings["google_model"])
        groq_validation = _validate_groq_key(next_settings["groq_api_key"], next_settings["groq_model"])

        next_settings["google_validation"] = google_validation
        next_settings["groq_validation"] = groq_validation

        save_settings(next_settings)
        _orchestrator.apply_settings(next_settings)

        return jsonify(
            {
                "ok": True,
                "saved": _saved_settings_payload(next_settings),
                "google_validation": google_validation,
                "groq_validation": groq_validation,
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

    @app.post("/api/prompts/revert")
    def api_prompts_revert():
        removed = clear_current_prompts()
        _orchestrator.reload_prompts()
        return jsonify({"ok": True, "removed": removed})

    @app.post("/api/start")
    def api_start():
        """
        Start a new session.
        Body: { n_turns, persona, pacing, model }
        """
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
        """Pause the current session."""
        return jsonify(_orchestrator.pause())

    @app.post("/api/resume")
    def api_resume():
        """Resume a paused session."""
        return jsonify(_orchestrator.resume())

    @app.post("/api/clear")
    def api_clear():
        """Wipe the current session."""
        return jsonify(_orchestrator.clear())

    @app.get("/api/poll")
    def api_poll():
        """
        Poll for newly displayed items.
        Query: ?since=N  (number of items already received by client)
        """
        since = request.args.get("since", 0, type=int)
        return jsonify(_orchestrator.poll(since_index=since))

    @app.get("/api/health")
    def api_health():
        """Check AI backend connectivity."""
        status = _orchestrator.big_connector.health_check()
        code = 200 if status["ok"] else 503
        status.update(_orchestrator.status)
        return jsonify(status), code

    _device = get_bridge()

    @app.post("/api/device/connect")
    def api_device_connect():
        body = request.get_json(silent=True) or {}
        url = body.get("url", "ws://localhost:8888")
        ok = _device.connect(url)
        return jsonify({"ok": ok, "url": url, "state": _device.latest_state})

    @app.post("/api/device/disconnect")
    def api_device_disconnect():
        _device.disconnect()
        return jsonify({"ok": True})

    @app.post("/api/device/home")
    def api_device_home():
        """Home the device by sending setZero."""
        _device.send({"cmd": "setZero"})
        return jsonify({"ok": True})

    @app.get("/api/device/state")
    def api_device_state():
        return jsonify({"ok": True, **_device.latest_state})

    @app.get("/api/device/stream")
    def api_device_stream():
        def generate():
            q = queue.Queue(maxsize=30)

            def on_data(data):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass

            _device.add_listener(on_data)
            try:
                yield f"data: {json.dumps({'type': 'position', **_device.latest_state})}\n\n"
                while True:
                    try:
                        data = q.get(timeout=2)
                        yield f"data: {json.dumps(data)}\n\n"
                    except queue.Empty:
                        yield f"data: {json.dumps({'type': 'ping'})}\n\n"
            finally:
                _device.remove_listener(on_data)

        response = Response(generate(), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @app.post("/api/device/command")
    def api_device_command():
        """Send a raw command dict to the device."""
        body = request.get_json(silent=True) or {}
        if "cmd" not in body:
            return jsonify({"ok": False, "error": "Missing cmd"}), 400
        _device.send(body)
        return jsonify({"ok": True})


def _validate_google_key(api_key: str, model: str) -> dict:
    connector = GoogleAIConnector(api_key=api_key, model=model)
    return connector.validate_api_key()


def _validate_groq_key(api_key: str, model: str) -> dict:
    connector = GroqAIConnector(api_key=api_key, model=model)
    return connector.validate_api_key()