"""
main.py
-------
Application entry point.

When run as a packaged (PyInstaller) binary, ``sys.executable`` is this app
rather than a Python interpreter, so the binary doubles as the launcher for the
bundled device emulator: ``AIMO --run-device-emulator --serial <port>`` is how
routes.py starts the emulator subprocess in a frozen build.
"""

import logging
import sys


def _run_device_emulator() -> None:
    """Hand off to the bundled device emulator (frozen-build subcommand)."""
    sys.argv.remove("--run-device-emulator")
    import device_emulator
    device_emulator.main()


def _maybe_open_browser(host: str, port: int) -> None:
    """Open the UI in the default browser shortly after the server starts.

    Enabled automatically for the packaged binary; opt in from source with
    AIMEE_OPEN_BROWSER=1.  Disable with AIMEE_NO_BROWSER=1.
    """
    import os

    if os.getenv("AIMEE_NO_BROWSER", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    frozen = getattr(sys, "frozen", False)
    if not frozen and os.getenv("AIMEE_OPEN_BROWSER", "").strip().lower() not in ("1", "true", "yes", "on"):
        return

    import threading
    import webbrowser

    # 0.0.0.0 isn't a connectable address; point the browser at localhost.
    visit_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{visit_host}:{port}"
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()


def main() -> None:
    if "--run-device-emulator" in sys.argv:
        _run_device_emulator()
        return

    from app_factory import create_app
    from config import FLASK_DEBUG, FLASK_HOST, FLASK_PORT

    app = create_app()
    logging.getLogger(__name__).info(
        "Starting OSSM Controller on %s:%d", FLASK_HOST, FLASK_PORT
    )
    _maybe_open_browser(FLASK_HOST, FLASK_PORT)
    # Never run the Werkzeug debugger in a packaged binary: it binds 0.0.0.0 and
    # the interactive debugger would be an RCE vector on any error page.
    debug = FLASK_DEBUG and not getattr(sys, "frozen", False)
    app.run(
        host=FLASK_HOST,
        port=FLASK_PORT,
        debug=debug,
        threaded=True,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
