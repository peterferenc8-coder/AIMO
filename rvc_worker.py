"""
rvc_worker.py
-------------
Persistent RVC voice-conversion worker.

Runs under Applio's own virtualenv (numpy 2.x, faiss, torchcrepe) which cannot
be merged into this app's environment, so it is driven as a subprocess over a
line-delimited JSON protocol on stdin/stdout.

Why a long-lived process rather than one CLI call per utterance:
  * ~17s of interpreter start + model load per invocation
  * Applio rebuilds the RMVPE pitch predictor for *every* conversion and throws
    it away (`del model` in rvc/infer/pipeline.py).  That alone measured 2.0-2.3s
    per call -- more than the entire cost of converting a short line.
Both are hoisted here, taking a typical short utterance from ~2.4s to ~0.2-0.4s.

Protocol (one JSON object per line):
    ->  {"id": 1, "input": "/tmp/a.wav", "output": "/tmp/b.wav", ...}
    <-  {"id": 1, "ok": true, "sample_rate": 40000, "duration_ms": 2420}
    <-  {"id": 1, "ok": false, "error": "..."}
On startup, once models are resident:
    <-  {"ready": true}
"""

import json
import os
import sys

# Applio resolves asset paths relative to the process CWD.
APPLIO_DIR = os.environ.get("APPLIO_DIR") or os.path.dirname(os.path.abspath(__file__))
os.chdir(APPLIO_DIR)
sys.path.insert(0, APPLIO_DIR)

# Applio prints progress banners and tqdm bars to stdout, which would corrupt
# the protocol stream.  Keep the real stdout private and point everything else
# at stderr.
_protocol_out = sys.stdout
sys.stdout = sys.stderr


def _send(obj):
    _protocol_out.write(json.dumps(obj) + "\n")
    _protocol_out.flush()


def _install_caches():
    """Hoist the two things Applio reloads on every conversion.

    Returns a short description of what was patched, for the ready message.
    """
    patched = []

    import rvc.infer.pipeline as pipeline

    # -- RMVPE pitch predictor (measured 2.0-2.3s per construction) -----------
    real_rmvpe = pipeline.RMVPE
    _rmvpe_cache = {}

    def cached_rmvpe(**kwargs):
        key = (str(kwargs.get("device")), kwargs.get("sample_rate"), kwargs.get("hop_size"))
        if key not in _rmvpe_cache:
            _rmvpe_cache[key] = real_rmvpe(**kwargs)
        return _rmvpe_cache[key]

    pipeline.RMVPE = cached_rmvpe
    patched.append("rmvpe")

    # -- FAISS index (~0.1s per read for a 77MB index) -----------------------
    import faiss

    real_read_index = faiss.read_index
    _index_cache = {}

    def cached_read_index(path, *a, **kw):
        if path not in _index_cache:
            _index_cache[path] = real_read_index(path, *a, **kw)
        return _index_cache[path]

    faiss.read_index = cached_read_index
    patched.append("faiss")

    return patched


def main():
    try:
        patched = _install_caches()
        from rvc.infer.infer import VoiceConverter

        converter = VoiceConverter()
    except Exception as exc:  # noqa: BLE001 - report anything to the parent
        _send({"ready": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1

    _send({"ready": True, "patched": patched})

    import soundfile as sf

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _send({"ok": False, "error": f"bad json: {exc}"})
            continue

        if req.get("cmd") == "shutdown":
            _send({"ok": True, "bye": True})
            return 0

        rid = req.get("id")
        try:
            converter.convert_audio(
                audio_input_path=req["input"],
                audio_output_path=req["output"],
                model_path=req["model"],
                index_path=req.get("index", ""),
                pitch=int(req.get("pitch", 0)),
                index_rate=float(req.get("index_rate", 0.7)),
                protect=float(req.get("protect", 0.33)),
                volume_envelope=float(req.get("volume_envelope", 1.0)),
                f0_method=req.get("f0_method", "rmvpe"),
                embedder_model=req.get("embedder_model", "contentvec"),
                split_audio=False,     # would alter length; visemes depend on it
                clean_audio=False,     # denoising strips the breath we want
                f0_autotune=False,
                export_format="WAV",
                sid=0,
            )
            info = sf.info(req["output"])
            _send({
                "id": rid,
                "ok": True,
                "sample_rate": info.samplerate,
                "duration_ms": round(info.duration * 1000),
            })
        except Exception as exc:  # noqa: BLE001
            _send({"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    return 0


if __name__ == "__main__":
    sys.exit(main())
