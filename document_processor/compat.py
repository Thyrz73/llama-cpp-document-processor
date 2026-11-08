"""Temporary adapters for import-app callers; factory applications never use them.

The supplied namespace is app.py's own globals dictionary. No package module
imports app, and this adapter is never installed in an injected application.
"""

from functools import partial

from .backend import CapabilityCache, DeploymentModelClient
from .jobs import JobStore
from .services import Services, make_snapshot, read_system_prompt
from .settings import Settings


def deployment_settings(namespace) -> Settings:
    """Adapt the legacy app globals without changing the frozen process snapshot.

    Existing monkeypatches remain effective. New code can pass Settings explicitly;
    keep this compatibility mapping here until callers stop patching the globals.
    Construction performs no environment/file reads or job-level revalidation.
    """
    return Settings(
        app_port=namespace["APP_PORT"],
        data_dir=namespace["DATA"],
        llama_base_url=namespace["LLAMA_BASE"],
        api_key=namespace["KEY"],
        request_timeout=namespace["REQUEST_TIMEOUT"],
        step_timeout=namespace["STEP_TIMEOUT"],
        ocr_timeout=namespace["OCR_TIMEOUT"],
        system_prompt_file=namespace["SYSTEM_FILE"],
        max_upload_mb=namespace["MAX_UPLOAD_MB"],
        max_pages=namespace["MAX_PAGES"],
        chunk_tokens=namespace["CHUNK_TOKENS"],
        context_tokens=namespace["CONTEXT_TOKENS"],
        output_language=namespace["OUTPUT_LANGUAGE"],
        analysis_tokens=namespace["ANALYSIS_TOKENS"],
        merge_tokens=namespace["MERGE_TOKENS"],
        final_tokens=namespace["FINAL_TOKENS"],
        max_model_calls=namespace["MAX_MODEL_CALLS"],
        max_job_attempts=namespace["MAX_JOB_ATTEMPTS"],
        live_output_enabled=namespace["LIVE_OUTPUT_ENABLED"],
        live_preview_chars=namespace["LIVE_PREVIEW_CHARS"],
        ocr_enabled=namespace["OCR_ENABLED"],
        ocr_languages=namespace["OCR_LANGUAGES"],
        ocr_dpi=namespace["OCR_DPI"],
        ocr_max_pixels=namespace["OCR_MAX_PIXELS"],
        thinking_adapter=namespace["THINKING_ADAPTER"],
        initial_thinking=namespace["INITIAL_THINKING"],
        apply_template_supported=namespace["APPLY_TEMPLATE_SUPPORTED"],
        usage_supported=namespace["USAGE_SUPPORTED"],
    )


def runtime_defaults(namespace, settings=None):
    return (
        deployment_settings(namespace) if settings is None else settings
    ).job_defaults()


def prompt(namespace, settings=None):
    return read_system_prompt(
        deployment_settings(namespace) if settings is None else settings
    )


def new_snapshot(namespace, options, capabilities=None, *, settings=None):
    defaults = (
        namespace["runtime_defaults"]()
        if settings is None
        else namespace["runtime_defaults"](settings)
    )
    reader = (
        namespace["read_system_prompt"]
        if settings is None
        else partial(namespace["read_system_prompt"], settings)
    )
    return make_snapshot(defaults, options, capabilities, reader)


class LegacyCapabilityCache(CapabilityCache):
    def __init__(self, namespace):
        self.namespace = namespace
        super().__init__(namespace["deployment_settings"])

    @property
    def entry(self):
        return self.namespace["_capability_cache"]

    @entry.setter
    def entry(self, value):
        self.namespace["_capability_cache"] = value

    @property
    def lock(self):
        return self.namespace["_capability_lock"]

    @lock.setter
    def lock(self, value):
        self.namespace["_capability_lock"] = value


class LegacyServices(Services):
    def __init__(self, namespace, settings, store):
        self.namespace = namespace
        super().__init__(settings, store)
        namespace.update(store=store, worker=self._worker)

    @property
    def settings(self):
        return self.namespace["deployment_settings"]()

    @property
    def store(self):
        return self.namespace["store"]

    @property
    def worker(self):
        return self.namespace["worker"]

    @property
    def client_factory(self):
        return self.namespace["ModelClient"]

    @property
    def backend_factory(self):
        return self.namespace["BackendClient"]

    def create_capabilities(self):
        return LegacyCapabilityCache(self.namespace)

    def runtime_defaults(self, settings=None):
        callback = self.namespace["runtime_defaults"]
        return callback() if settings is None else callback(settings)

    def read_system_prompt(self):
        return self.namespace["read_system_prompt"]()

    def new_snapshot(self, options, capabilities=None):
        return self.namespace["new_snapshot"](options, capabilities)


def legacy_client(namespace, capabilities):
    class LegacyDeploymentClient(DeploymentModelClient):
        def __init__(self, config=None, *, settings=None):
            deployment = (
                namespace["deployment_settings"]() if settings is None else settings
            )
            if config is None:
                callback = namespace["runtime_defaults"]
                config = callback() if settings is None else callback(deployment)
            prompt = config.get("system_prompt")
            if prompt is None:
                reader = namespace["read_system_prompt"]
                prompt = reader() if settings is None else reader(deployment)
            super().__init__(
                deployment.llama_base_url,
                deployment.api_key,
                config,
                prompt,
                deployment.live_preview_chars,
                capabilities=capabilities,
            )

    return LegacyDeploymentClient


def install_legacy(namespace, settings):
    """Export only the names still used by legacy tests, keeping their patch behavior."""
    namespace.update(
        APP_PORT=settings.app_port,
        DATA=settings.data_dir,
        LLAMA_BASE=settings.llama_base_url,
        KEY=settings.api_key,
        REQUEST_TIMEOUT=settings.request_timeout,
        STEP_TIMEOUT=settings.step_timeout,
        OCR_TIMEOUT=settings.ocr_timeout,
        SYSTEM_FILE=settings.system_prompt_file,
        MAX_UPLOAD_MB=settings.max_upload_mb,
        MAX_PAGES=settings.max_pages,
        CHUNK_TOKENS=settings.chunk_tokens,
        CONTEXT_TOKENS=settings.context_tokens,
        OUTPUT_LANGUAGE=settings.output_language,
        ANALYSIS_TOKENS=settings.analysis_tokens,
        MERGE_TOKENS=settings.merge_tokens,
        FINAL_TOKENS=settings.final_tokens,
        MAX_MODEL_CALLS=settings.max_model_calls,
        MAX_JOB_ATTEMPTS=settings.max_job_attempts,
        LIVE_OUTPUT_ENABLED=settings.live_output_enabled,
        LIVE_PREVIEW_CHARS=settings.live_preview_chars,
        OCR_ENABLED=settings.ocr_enabled,
        OCR_LANGUAGES=settings.ocr_languages,
        OCR_DPI=settings.ocr_dpi,
        OCR_MAX_PIXELS=settings.ocr_max_pixels,
        THINKING_ADAPTER=settings.thinking_adapter,
        INITIAL_THINKING=settings.initial_thinking,
        APPLY_TEMPLATE_SUPPORTED=settings.apply_template_supported,
        USAGE_SUPPORTED=settings.usage_supported,
    )
    namespace.update(
        deployment_settings=partial(deployment_settings, namespace),
        runtime_defaults=partial(runtime_defaults, namespace),
        read_system_prompt=partial(prompt, namespace),
        new_snapshot=partial(new_snapshot, namespace),
    )
    services = LegacyServices(namespace, settings, JobStore(settings.data_dir))
    namespace.update(
        ModelClient=legacy_client(namespace, services.capabilities),
        cached_capabilities=services.capabilities.get,
        remember_capabilities=services.capabilities.remember,
        invalidate_capabilities=services.capabilities.invalidate,
        capability_backend=services.capabilities.backend,
        analyze_job=services.analyze_job,
        TRANSLATIONS=services.translations,
        DEFAULT_QUESTIONS=services.default_questions,
        FALLBACK_QUESTION=services.fallback_question,
        MAX_QUESTION_CHARS=services.max_question_chars,
    )
    return services
