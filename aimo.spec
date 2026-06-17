# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build spec for AIMO.

Produces a single self-contained executable (`AIMO` / `AIMO.exe`) that bundles
the Python interpreter, all dependencies, and the read-only data the app reads
at runtime (prompts, intents, built-in patterns, templates, static assets, and
the device emulator script).

Build:  pyinstaller aimo.spec
Output: dist/AIMO            (Linux/macOS)
        dist/AIMO.exe        (Windows)

Writable user data (settings, custom patterns, uploaded funscripts/videos,
logs) is NOT bundled — at runtime it lives under ~/.config/aimee (see config.py
RESOURCE_DIR vs DATA_DIR), so the read-only bundle is never written to.
"""

from glob import glob
from PyInstaller.utils.hooks import collect_all

# ── Read-only data shipped inside the binary ─────────────────────────────────
# (source path on disk, destination path inside the bundle root == RESOURCE_DIR)
datas = [
    ("prompts", "prompts"),
    ("intents", "intents"),
    ("templates", "templates"),
    ("static", "static"),
    ("device_emulator.py", "."),
]
# Built-in motion patterns are the top-level *.json files only; the custom/,
# funscripts/, and videos/ subfolders are user data and must stay out.
for json_path in glob("patterns/*.json"):
    datas.append((json_path, "patterns"))

binaries = []
hiddenimports = []

# ── Pull in packages with data files / dynamic imports PyInstaller can miss ───
# google.genai carries bundled schema data; bleak loads backend submodules
# dynamically. collect_all grabs datas + binaries + submodules.
for pkg in (
    "bleak",
    "google.genai",
):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    except Exception:
        # Optional package not installed in this build environment — skip it.
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# pyserial / websockets are imported by string in places; name them explicitly.
hiddenimports += ["serial", "serial.tools.list_ports", "websockets"]

# Text-to-speech (Kokoro + PyTorch) is intentionally NOT bundled — it is far too
# large to ship and is provided as a source-only extra (see requirements-tts.txt).
# Exclude the whole stack so it is never pulled in, even if it happens to be
# installed in the build environment. tts.py imports these lazily, so the app
# still runs; TTS just reports that it is unavailable until installed from source.
TTS_EXCLUDES = ["torch", "kokoro", "misaki", "soundfile", "numpy", "scipy"]


block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"] + TTS_EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="AIMO",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
