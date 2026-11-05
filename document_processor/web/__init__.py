"""HTTP route groups; dependencies belong to the current Flask application."""

from flask import abort, current_app

from ..jobs import InvalidJobPath
from ..services import Services


def get_services() -> Services:
    return current_app.extensions["document_processor"]


def job_directory(job_id):
    """Use the store's validated server-generated path, or answer HTTP 404."""
    try:
        return get_services().store.job_dir(job_id)
    except InvalidJobPath:
        abort(404)
