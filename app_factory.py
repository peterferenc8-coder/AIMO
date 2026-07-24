"""
app_factory.py
--------------
Application factory. Keeps Flask setup isolated so routes can be
registered independently and the app can be created on demand
(for tests, CLI commands, etc.).
"""

import logging
import sys
from flask import Flask

from config import LOG_LEVEL, STATIC_DIR, TEMPLATES_DIR
from routes import register_routes
from devices.registry import set_active_device


def create_app() -> Flask:
    """Configure logging, create the Flask app, and attach all routes."""
    _setup_logging()
    # Point Flask at explicit template/static folders so they resolve correctly
    # both from source and from a PyInstaller bundle (where they are unpacked
    # under sys._MEIPASS rather than next to this module).
    app = Flask(
        __name__,
        template_folder=str(TEMPLATES_DIR),
        static_folder=str(STATIC_DIR),
    )
    register_routes(app)
    # Initialize default device on startup. "none" so the app is usable before
    # any toy is paired; the user picks real hardware on the Setup tab.
    try:
        set_active_device("none")
    except Exception as exc:
        logging.getLogger(__name__).warning("Failed to initialize default device: %s", exc)
    _prewarm_tts()
    return app


def _prewarm_tts() -> None:
    """Load the Kokoro and RVC models in the background as the server comes up.

    Both are otherwise loaded on first use, inside the /api/start request that
    synthesises the opening turns -- so the user pays ~30s of model loading
    watching a blank screen.  The server binds its port immediately either way;
    this thread just races the user's first click, and usually wins it.
    """
    import threading

    def _run() -> None:
        try:
            import tts
            tts.prewarm()
        except Exception as exc:  # noqa: BLE001 - boot must never fail on this
            logging.getLogger(__name__).info("TTS prewarm unavailable: %s", exc)

    threading.Thread(target=_run, name="tts-prewarm", daemon=True).start()


def _setup_logging() -> None:
    """Configure root logger format and level once."""
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        stream=sys.stdout,
    )
