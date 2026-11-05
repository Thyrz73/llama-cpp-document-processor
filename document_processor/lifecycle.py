"""Explicit server lifecycle: validate, initialize, serve, then stop and checkpoint."""

import signal
import sys

from . import config as job_config
from .database import SchemaVersionError
from .observability import event, log


def run_server(app, services, serve, settings):
    event("processor_initializing", status="starting")
    try:
        job_config.resolve(services.runtime_defaults())
    except job_config.ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    try:
        services.store.initialize()
    except SchemaVersionError as exc:
        raise SystemExit(f"Database error: {exc}") from None
    # SIGTERM also interrupts a running job cooperatively, without cancelling it permanently.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    try:
        services.worker.start()
        log.info("processor_started port=%s", settings.app_port)
        serve(app, host="0.0.0.0", port=settings.app_port, threads=4)
    finally:
        stopped = services.worker.stop()
        services.store.close()
        event("processor_stopped", status="stopped", worker_stopped=stopped)
