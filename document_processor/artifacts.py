"""Reuse intermediate results across retries, but only when they are verified.

Each job keeps its artifacts (extracted text, section notes, merges, final
report) under ``artifacts/<configuration fingerprint>/`` with a manifest that
records, for every file, the fingerprint of its inputs and the SHA-256 of its
content. A retry reuses a file only when both still match, so a changed
input, model or tampered file always leads to recomputation.
"""

import json
from pathlib import Path

from .config import fingerprint
from .errors import ProcessingError
from .files import atomic_json, atomic_text, file_hash


class Artifacts:
    """The intermediate results of one job configuration, with their manifest.

    write() saves a file atomically, then records it in the manifest; read()
    returns it only when the manifest entry matches both its dependencies and
    its content. A crash between the two leads to recomputation, never to the
    reuse of an unverified file. Files left at the job root by older versions
    are never deleted.
    """

    def __init__(self, directory, config):
        self.directory = Path(directory)
        self.config_hash = fingerprint(config)
        self.root = self.directory / "artifacts" / self.config_hash
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "manifest.json"
        self.manifest = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
            "version": 1, "config_hash": self.config_hash, "artifacts": {}, "identity": None}
        if self.manifest.get("config_hash") != self.config_hash or self.manifest.get("version") != 1:
            raise ProcessingError("artifacts_incompatible", "Artifact manifest is incompatible with this job.")

    def bind_identity(self, identity):
        """Refuse changed or unverifiable identities before reusing existing artifacts."""
        previous = self.manifest.get("identity")
        required = ("model_id_hash", "template_hash", "engine_sampling_hash")
        unverified = bool(self.manifest["artifacts"]) and any(
            (previous or {}).get(key) is None or identity.get(key) is None for key in required)
        if unverified or (previous and any(previous.get(key) is not None and identity.get(key) != previous[key]
                                          for key in previous)):
            raise ProcessingError("identity_changed", "The model or template changed. Start a new analysis.")
        self.manifest["identity"] = identity
        atomic_json(self.path, self.manifest)

    def read(self, name, dependencies):
        """Return verified text, or None when there is no reusable completed artifact."""
        entry = self.manifest["artifacts"].get(name)
        path = self.root / name
        if (not entry or entry.get("complete") is not True or entry.get("dependencies") != fingerprint(dependencies)
                or not path.is_file() or file_hash(path) != entry.get("sha256")):
            return None
        return path.read_text(encoding="utf-8")

    def write(self, name, text, dependencies):
        """Publish contents first, then the matching completed manifest entry."""
        path = self.root / name
        atomic_text(path, text)
        self.manifest["artifacts"][name] = {
            "dependencies": fingerprint(dependencies), "sha256": file_hash(path), "complete": True}
        atomic_json(self.path, self.manifest)
