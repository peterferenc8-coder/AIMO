"""
main.py
-------
Application entry point.
"""

import logging

from app_factory import create_app
from config import FLASK_DEBUG, FLASK_HOST, FLASK_PORT

if __name__ == "__main__":
    app = create_app()
    logging.getLogger(__name__).info(
        "Starting OSSM Controller on %s:%d", FLASK_HOST, FLASK_PORT
    )
    app.run(
        host=FLASK_HOST,
        port=FLASK_PORT,
        debug=FLASK_DEBUG,
        threaded=True,
        use_reloader=False,
    )