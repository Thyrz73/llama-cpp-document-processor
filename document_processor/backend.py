"""Per-application backend observations and the deployment-bound model client."""

import copy
import hashlib
import threading
import time
from contextlib import contextmanager
from typing import Any

from .errors import ModelError
from .model_client import ModelClient as BackendClient

CAPABILITY_CACHE_SECONDS = 30
CAPABILITY_FAILURE_CACHE_SECONDS = 5


class CapabilityCache:
    """One backend-keyed TTL cache and inspection lock per application."""

    def __init__(self, settings_provider):
        self.settings_provider = settings_provider
        self.entry: dict[str, Any] = {"value": None, "expires": 0}
        self.lock = threading.Lock()

    def backend(self, client=None):
        """Identify the configured backend privately, without retaining another credential copy."""
        if client is None:
            settings = self.settings_provider()
            base = settings.llama_base_url
            authorization = f"Bearer {settings.api_key}" if settings.api_key else ""
        else:
            base = client.base_url
            authorization = client.session.headers.get("Authorization", "")
        return base.rstrip("/"), hashlib.sha256(authorization.encode("utf-8")).digest()

    def get(self, client=None):
        """Return an unexpired inspection for this backend, without network I/O.

        Complete successes live for 30 seconds; failures and unknown contexts
        live for 5 seconds. Reads never extend the original expiration.
        """
        cached = self.entry
        if (
            cached.get("backend") == self.backend(client)
            and time.monotonic() < cached["expires"]
        ):
            return copy.deepcopy(cached["value"])
        return None

    def remember(self, value, client=None):
        """Publish one complete cache entry; callers serialize inspections with the lock."""
        context = value.get("context_tokens")
        healthy = (
            value.get("status") == "available" and type(context) is int and context > 0
        )
        ttl = CAPABILITY_CACHE_SECONDS if healthy else CAPABILITY_FAILURE_CACHE_SECONDS
        self.entry = {
            "value": copy.deepcopy(value),
            "expires": time.monotonic() + ttl,
            "backend": self.backend(client),
            "model_name": client.model_name if client is not None else None,
        }

    def invalidate(self, client):
        """Discard observations after a backend failure, leaving cancellation independent."""
        if self.entry.get("backend") == self.backend(client):
            self.entry = {"value": None, "expires": 0}

    @contextmanager
    def inspection(self, client):
        """Own inspection synchronization, including cancellable acquisition."""
        client.check_cancelled()
        while not self.lock.acquire(timeout=0.2):
            client.check_cancelled()
        try:
            client.check_cancelled()
            yield
        finally:
            self.lock.release()

    def diagnostics(self, create_client):
        """Return (observation, refreshed) without acquiring a client's lock twice.

        Clients are created only on misses. Recheck under the shared lock so
        concurrent diagnostics coalesce, including raw injected clients. A client
        bound to this cache uses its already-locked inspection implementation.
        Identity inspections keep their separate, always-fresh entry point.
        """
        value = self.get()
        if value is not None:
            return value, False
        client = create_client()
        close_pending = True
        try:
            with self.inspection(client):
                value = self.get()
                if value is not None:
                    return value, False
                bound = (
                    isinstance(client, DeploymentModelClient) and client._cache is self
                )
                if bound:
                    value = client._inspect_capabilities_locked(for_identity=False)
                else:
                    value = client.inspect()
                # Raw clients retain the original close-before-publication order.
                close_pending = False
                client.session.close()
                if not bound:
                    self.remember(value, client)
                return value, True
        finally:
            if close_pending:
                client.session.close()


class DeploymentModelClient(BackendClient):
    """Bind the reusable backend adapter to this application's deployment settings."""

    def __init__(
        self, base_url, key, config, prompt, preview_limit=12000, *, capabilities
    ):
        self._cache = capabilities
        super().__init__(base_url, key, config, prompt, preview_limit)

    def inspect(self, *, cached=None):
        """Read capabilities for display, allowing the shared TTL cache."""
        return self._inspect_capabilities(for_identity=False)

    def inspect_identity(self):
        """Verify a new attempt's identity without using UI cache entries."""
        return self._inspect_capabilities(for_identity=True)

    def _inspect_capabilities(self, *, for_identity):
        """Serialize observations and publish fresh identity checks to the UI cache."""
        with self._cache.inspection(self):
            return self._inspect_capabilities_locked(for_identity=for_identity)

    def _inspect_capabilities_locked(self, *, for_identity):
        """Inspect with this client's cache lock already held by CapabilityCache."""
        previous = None if for_identity else self._cache.get(self)
        if previous is not None and previous.get("status") == "unavailable":
            self.capabilities = previous
            self.model_name = None
            return previous
        model_name = (
            self._cache.entry.get("model_name") if previous is not None else None
        )
        if for_identity:
            try:
                value = super().inspect_identity()
            except ModelError:
                self._cache.remember(self.capabilities, self)
                raise
        else:
            value = super().inspect(
                cached=(model_name, previous) if previous is not None else None
            )
        if previous is None or self.model_name != model_name or value != previous:
            self._cache.remember(value, self)
        return value

    def json_request(self, *args, **kwargs):
        """Invalidate cached properties on failed or malformed backend requests."""
        try:
            return super().json_request(*args, **kwargs)
        except ModelError:
            self._cache.invalidate(self)
            raise

    def generate(self, *args, **kwargs):
        """Do not retain capabilities after a generation fails at the backend."""
        try:
            return super().generate(*args, **kwargs)
        except ModelError:
            self._cache.invalidate(self)
            raise

    def tokenize(self, text):
        """Discard capabilities if tokenization fails before generation starts."""
        try:
            return super().tokenize(text)
        except ModelError:
            self._cache.invalidate(self)
            raise
