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
    """Validate artifacts and publish a manifest after each atomic file write.

    Legacy root files are never deleted. V2 work lives in a snapshot-specific
    directory, while the completed report is still published at its old path.
    A crash before manifest publication causes recomputation, never unsafe reuse.
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
        """Refuse known model/template changes before reusing generated notes."""
        previous = self.manifest.get("identity")
        if previous and any(previous.get(key) is not None and identity.get(key) != previous[key]
                            for key in previous):
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
