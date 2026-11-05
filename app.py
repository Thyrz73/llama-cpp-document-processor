"""Compatibility entry point and Waitress wiring.

Importing this module constructs one default application without opening SQLite,
creating HTTP clients or starting a worker. New callers should use create_app()
with injected settings/services; legacy monkeypatches are confined to compat.py.
"""

import time as time

from waitress import serve

from document_processor import environment
from document_processor.application import _create_app
from document_processor.application import create_app as create_app
from document_processor.backend import (
    CAPABILITY_CACHE_SECONDS as CAPABILITY_CACHE_SECONDS,
)
from document_processor.backend import (
    CAPABILITY_FAILURE_CACHE_SECONDS as CAPABILITY_FAILURE_CACHE_SECONDS,
)
from document_processor.backend import BackendClient as BackendClient
from document_processor.compat import install_legacy
from document_processor.lifecycle import run_server
from document_processor.observability import configure_logging
from document_processor.observability import log as log
from document_processor.settings import Settings
from document_processor.web.hooks import too_large as too_large

try:
    SETTINGS = Settings.from_env()
except environment.SettingsError as exc:
    raise SystemExit(f"Configuration error: {exc}") from None

configure_logging()
services = install_legacy(globals(), SETTINGS)
app = _create_app(services)

if __name__ == "__main__":
    run_server(app, services, serve, SETTINGS)
