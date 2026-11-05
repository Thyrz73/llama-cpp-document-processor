"""Application collaborators shared by HTTP routes and the background worker."""

import json
from collections.abc import Callable
from pathlib import Path

from . import config as job_config
from . import pipeline, prompts
from .backend import BackendClient, CapabilityCache, DeploymentModelClient
from .jobs import JobStore
from .settings import Settings
from .worker import Worker

ROOT = Path(__file__).resolve().parent.parent
FALLBACK_QUESTION = "Analyze the entire document"
MAX_QUESTION_CHARS = 1500


def read_system_prompt(settings):
    """Read the system prompt file and strip surrounding whitespace.

    New jobs save this text in their snapshot; existing jobs keep their copy.
    Raise ConfigurationError when the file is missing or unreadable.
    """
    try:
        return settings.system_prompt_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise job_config.ConfigurationError(
            "missing_prompt", "The system prompt is missing or unreadable."
        ) from None


def make_snapshot(defaults, options, capabilities, prompt_reader):
    """Resolve options into the configuration snapshot of a new job.

    The snapshot includes the current system prompt and task templates, so a
    retry uses the same text even after these files change. Raise
    ConfigurationError for invalid options or an empty system prompt.
    """
    config = job_config.resolve(defaults, options, capabilities)
    config.update(
        system_prompt=prompt_reader(),
        templates=dict(prompts.TEMPLATES),
        prompt_version=prompts.PROMPT_VERSION,
    )
    if not config["system_prompt"]:
        raise job_config.ConfigurationError(
            "missing_prompt", "The system prompt is empty."
        )
    return config


class Services:
    """Own state per application; constructing services never starts work or opens SQLite."""

    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        client_factory: Callable[[dict], BackendClient] | None = None,
        worker: Worker | None = None,
    ):
        self._settings = settings
        self._store = store
        self._client_factory = client_factory
        self.capabilities = self.create_capabilities()
        self.translations = json.loads(
            (ROOT / "static/translations.json").read_text(encoding="utf-8")
        )
        self.fallback_question = FALLBACK_QUESTION
        self.max_question_chars = MAX_QUESTION_CHARS
        self.default_questions = {
            self.translations[language]["default_question"] for language in ("en", "fr")
        } | {self.fallback_question}
        self._worker = Worker(store, self.analyze_job) if worker is None else worker

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def store(self) -> JobStore:
        return self._store

    @property
    def worker(self) -> Worker:
        return self._worker

    @property
    def backend_factory(self):
        return BackendClient

    @property
    def client_factory(self):
        return (
            self.make_client if self._client_factory is None else self._client_factory
        )

    def create_capabilities(self):
        return CapabilityCache(lambda: self.settings)

    def runtime_defaults(self, settings=None):
        return (self.settings if settings is None else settings).job_defaults()

    def read_system_prompt(self):
        return read_system_prompt(self.settings)

    def new_snapshot(self, options, capabilities=None):
        return make_snapshot(
            self.runtime_defaults(), options, capabilities, self.read_system_prompt
        )

    def make_client(self, config=None) -> DeploymentModelClient:
        settings = self.settings
        config = self.runtime_defaults() if config is None else config
        prompt = config.get("system_prompt")
        if prompt is None:
            prompt = self.read_system_prompt()
        return DeploymentModelClient(
            settings.llama_base_url,
            settings.api_key,
            config,
            prompt,
            settings.live_preview_chars,
            capabilities=self.capabilities,
        )

    def diagnostic_client(self):
        """Inspect without reading a private prompt; honor an injected client factory."""
        defaults = self.runtime_defaults(self.settings)
        if self._client_factory is not None:
            return self._client_factory({**defaults, "system_prompt": ""})
        return self.backend_factory(
            self.settings.llama_base_url, self.settings.api_key, defaults, ""
        )

    def is_default_question(self, question):
        """Tell whether a request is one of the interface's default instructions."""
        return question.strip() in self.default_questions

    def public_job(self, job):
        """Apply the default-instructions rule to jobs saved before it was recorded.

        Their question_custom stays null unless the request is a known default,
        so the browser never needs its own copy of the default questions.
        """
        if (
            job.get("question_custom") is None
            and job.get("question")
            and self.is_default_question(job["question"])
        ):
            job["question_custom"] = 0
        return job

    def analyze_job(self, job_id, cancellation_requested=None):
        """Run the analysis pipeline for job_id with this deployment's services.

        The worker passes its own cancellation check; without one, every check
        reads SQLite.
        """
        settings = self.settings
        pipeline.run(
            job_id,
            pipeline.Runtime(
                store=self.store,
                new_snapshot=self.new_snapshot,
                model_client=self.client_factory,
                cancellation_requested=cancellation_requested
                or self.store.read_cancellation,
                live_output=settings.live_output_enabled,
                preview_chars=settings.live_preview_chars,
            ),
        )
