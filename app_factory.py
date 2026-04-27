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

from config import LOG_LEVEL
from routes import register_routes


def create_app() -> Flask:
    """Configure logging, create the Flask app, and attach all routes."""
    _setup_logging()
    app = Flask(__name__)
    register_routes(app)
    return app


def _setup_logging() -> None:
    """Configure root logger format and level once."""
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        stream=sys.stdout,
    )