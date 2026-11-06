"""Build an isolated Flask application; starting its server is a separate operation."""

from collections.abc import Callable

from flask import Flask, request

from .backend import BackendClient
from .contracts import JobConfig
from .jobs import JobStore
from .observability import failure
from .services import ROOT, Services
from .settings import Settings
from .web import jobs, reports, system
from .web.hooks import register_hooks
from .worker import Worker


class ProcessorFlask(Flask):
    def log_exception(self, exc_info):
        """Prevent Flask's default traceback logger from leaking exception bodies."""
        failure(
            "request_failed",
            exc_info[1],
            endpoint=request.endpoint.rsplit(".", 1)[-1] if request.endpoint else None,
        )


def create_app(
    settings: Settings,
    store: JobStore | None = None,
    client_factory: Callable[[JobConfig], BackendClient] | None = None,
    *,
    worker: Worker | None = None,
) -> Flask:
    """Inject deployment settings, storage and optional client/worker test doubles.

    This creates routes and services only. It does not validate joint job budgets,
    initialize storage, create a client, start a worker, or contact the backend.
    """
    store = JobStore(settings.data_dir) if store is None else store
    return _create_app(Services(settings, store, client_factory, worker))


def _create_app(services: Services) -> Flask:
    """Shared construction path for injected applications and the legacy adapter."""
    app = ProcessorFlask(
        "document_processor", root_path=str(ROOT), instance_path=str(ROOT / "instance")
    )
    app.config["MAX_CONTENT_LENGTH"] = services.settings.max_content_length
    app.extensions["document_processor"] = services
    for blueprint in (system.blueprint, jobs.blueprint, reports.blueprint):
        app.register_blueprint(blueprint)
    register_hooks(app)
    return app
