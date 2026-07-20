"""
rvc_client.py
-------------
Client for the persistent RVC voice-conversion worker (see rvc_worker.py).

Named rvc_client rather than rvc deliberately: Applio's own package is also
called `rvc` and ships without an __init__.py, making it a namespace package.
A regular module named rvc.py anywhere on sys.path silently wins that import
and breaks the worker.

Applio needs its own virtualenv, so conversion runs in a subprocess.  This
module owns that process: lazy start, request/response, and -- importantly --
graceful degradation.  If RVC is unavailable or fails, callers fall back to
plain Kokoro audio rather than losing speech entirely.

RVC is frame-synchronous: one input frame maps to one output frame, so the
converted audio has the same duration as the input (within a couple of ~10ms
frames).  That is what lets tts.py keep Kokoro's word timings and viseme track
untouched instead of realigning them.
"""

import atexit
import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import config

log = logging.getLogger(__name__)

# Model load takes ~20s on a GTX 1060; conversion itself is well under a second.
_START_TIMEOUT = 180.0
_CONVERT_TIMEOUT = 120.0

_proc: subprocess.Popen | None = None
_lock = threading.Lock()
_req_id = 0
_disabled_reason: str | None = None


def configure(enabled: bool | None = None, pitch: int | None = None,
              index_rate: float | None = None) -> None:
    """Apply settings live.  Values land on the config module, which is what
    convert() reads per request, so changes take effect on the next utterance
    without restarting the worker."""
    global _disabled_reason

    if enabled is not None:
        was = config.RVC_ENABLED
        config.RVC_ENABLED = bool(enabled)
        if config.RVC_ENABLED and not was:
            # Re-arm after a previous failure so toggling off/on retries.
            _disabled_reason = None
    if pitch is not None:
        try:
            config.RVC_PITCH = int(pitch)
        except (TypeError, ValueError):
            pass
    if index_rate is not None:
        try:
            config.RVC_INDEX_RATE = float(index_rate)
        except (TypeError, ValueError):
            pass


def _paths_ok() -> str | None:
    """Return a reason string if RVC cannot run, else None."""
    if not config.RVC_ENABLED:
        return "disabled in config"
    for label, p in (("python", config.RVC_PYTHON),
                     ("applio dir", config.RVC_APPLIO_DIR),
                     ("model", config.RVC_MODEL)):
        if not os.path.exists(p):
            return f"{label} not found: {p}"
    return None


def available() -> bool:
    """True if conversion is currently possible (does not start the worker)."""
    return _disabled_reason is None and _paths_ok() is None


def status() -> dict[str, Any]:
    return {
        "enabled": config.RVC_ENABLED,
        "running": _proc is not None and _proc.poll() is None,
        "reason": _disabled_reason or _paths_ok(),
        "model": os.path.basename(config.RVC_MODEL),
        "pitch": config.RVC_PITCH,
    }


def _start() -> bool:
    """Spawn the worker and wait for its ready handshake."""
    global _proc, _disabled_reason

    reason = _paths_ok()
    if reason:
        _disabled_reason = reason
        log.warning("RVC unavailable (%s); using plain Kokoro audio", reason)
        return False

    worker = str(Path(__file__).resolve().parent / "rvc_worker.py")
    env = dict(os.environ, APPLIO_DIR=config.RVC_APPLIO_DIR)

    log.info("Starting RVC worker (this loads the model, ~20s)...")
    try:
        _proc = subprocess.Popen(
            [config.RVC_PYTHON, worker],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=env, cwd=config.RVC_APPLIO_DIR,
        )
    except OSError as exc:
        _disabled_reason = f"spawn failed: {exc}"
        log.warning("RVC worker could not start: %s", exc)
        return False

    ready = _read_line(_START_TIMEOUT)
    if not ready or not ready.get("ready"):
        err = (ready or {}).get("error", "no handshake")
        _disabled_reason = f"worker init failed: {err}"
        log.warning("RVC worker failed to initialise: %s", err)
        _kill()
        return False

    log.info("RVC worker ready (cached: %s)", ", ".join(ready.get("patched", [])))
    return True


def _read_line(timeout: float) -> dict[str, Any] | None:
    """Read one JSON line from the worker, with a timeout."""
    if _proc is None or _proc.stdout is None:
        return None

    result: list[str] = []

    def _reader():
        try:
            line = _proc.stdout.readline()
            if line:
                result.append(line)
        except Exception:  # noqa: BLE001 - pipe closed mid-read
            pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout)
    if not result:
        return None
    try:
        return json.loads(result[0])
    except json.JSONDecodeError:
        return None


def _kill() -> None:
    global _proc
    if _proc is not None:
        try:
            _proc.kill()
        except Exception:  # noqa: BLE001
            pass
        _proc = None


def convert(input_path: str, output_path: str) -> dict[str, Any] | None:
    """Convert *input_path* to the target voice, writing *output_path*.

    Returns the worker's result dict, or None if conversion was unavailable or
    failed -- in which case the caller should keep the original audio.
    """
    global _proc, _req_id, _disabled_reason

    if _disabled_reason is not None:
        return None

    with _lock:
        if _proc is None or _proc.poll() is not None:
            if _proc is not None:
                log.warning("RVC worker died (rc=%s); restarting", _proc.returncode)
                _proc = None
            if not _start():
                return None

        _req_id += 1
        req = {
            "id": _req_id,
            "input": input_path,
            "output": output_path,
            "model": config.RVC_MODEL,
            "index": config.RVC_INDEX if os.path.exists(config.RVC_INDEX) else "",
            "pitch": config.RVC_PITCH,
            "index_rate": config.RVC_INDEX_RATE,
            "protect": config.RVC_PROTECT,
        }

        try:
            _proc.stdin.write(json.dumps(req) + "\n")
            _proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            log.warning("RVC worker pipe broke: %s", exc)
            _kill()
            return None

        resp = _read_line(_CONVERT_TIMEOUT)

    if resp is None:
        log.warning("RVC worker timed out; falling back to Kokoro audio")
        _kill()
        return None
    if not resp.get("ok"):
        log.warning("RVC conversion failed: %s", resp.get("error"))
        return None
    return resp


def shutdown() -> None:
    """Stop the worker (registered atexit; safe to call repeatedly).

    The worker holds the RVC model and RMVPE predictor in VRAM, so leaving it
    orphaned would keep ~1.5GB reserved on the GPU after the app exits.
    """
    global _proc
    with _lock:
        if _proc is None or _proc.poll() is not None:
            _proc = None
            return
        try:
            _proc.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
            _proc.stdin.flush()
            _proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            _kill()
        finally:
            _proc = None


# The worker keeps the model resident in VRAM; make sure it dies with us.
atexit.register(shutdown)
