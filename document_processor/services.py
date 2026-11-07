"""Application collaborators shared by HTTP routes and the background worker."""

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

from . import config as job_config
from . import pipeline, prompts
from .backend import BackendClient, CapabilityCache, DeploymentModelClient
from .contracts import DiagnosticConfig, JobConfig, JobSnapshot
from .jobs import JobStore
from .observability import event
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


def make_snapshot(
    defaults: JobConfig,
    options: object,
    capabilities: Mapping[str, object] | None,
    prompt_reader: Callable[[], str],
) -> JobSnapshot:
    """Resolve options into the configuration snapshot of a new job.

    The snapshot includes the current system prompt and task templates, so a
    retry uses the same text even after these files change. Raise
    ConfigurationError for invalid options or an empty system prompt.
    """
    # Extend the validated controls in place; the required prompt fields are added below.
    config: JobSnapshot = cast(
        JobSnapshot, job_config.resolve(defaults, options, capabilities)
    )
    # mypy does not accept keyword updates on a TypedDict; retain dict.update semantics.
    cast(dict[str, object], config).update(
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
        client_factory: Callable[[JobConfig], BackendClient] | None = None,
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
    def client_factory(self) -> Callable[[JobConfig], BackendClient]:
        return (
            self.make_client if self._client_factory is None else self._client_factory
        )

    def create_capabilities(self):
        return CapabilityCache(lambda: self.settings)

    def runtime_defaults(self, settings: Settings | None = None) -> JobConfig:
        return (self.settings if settings is None else settings).job_defaults()

    def read_system_prompt(self):
        return read_system_prompt(self.settings)

    def new_snapshot(
        self, options: object, capabilities: Mapping[str, object] | None = None
    ) -> JobSnapshot:
        return make_snapshot(
            self.runtime_defaults(), options, capabilities, self.read_system_prompt
        )

    def make_client(self, config: JobConfig | None = None) -> DeploymentModelClient:
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
            diagnostic: DiagnosticConfig = {**defaults, "system_prompt": ""}
            return self._client_factory(diagnostic)
        return self.backend_factory(
            self.settings.llama_base_url, self.settings.api_key, defaults, ""
        )

    def inspect_diagnostics(self):
        """Resolve display observations through the cache's inspection boundary."""
        value, refreshed = self.capabilities.diagnostics(self.diagnostic_client)
        if refreshed:
            event(
                "backend_checked",
                status=value.get("status", "unknown"),
                source="diagnostics",
            )
        return value

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
