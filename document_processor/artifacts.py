"""Reuse intermediate results across retries, but only when they are verified.

Each job keeps its artifacts (extracted text, section notes, merges, final
report) under ``artifacts/<configuration fingerprint>/`` with a manifest that
records, for every file, the fingerprint of its inputs and the SHA-256 of its
content. A retry reuses a file only when both still match, so a changed
input, model or tampered file always leads to recomputation.
"""

import copy
import hashlib
import json
import os
import re
import stat
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Literal, cast

from .config import RESUME_BUDGETS, compatible_artifact_config, fingerprint
from .contracts import ArtifactHistory
from .errors import ProcessingError
from .files import atomic_json, atomic_text, file_hash
from .jobs import MAX_ARTIFACT_REVISIONS, SOURCE_FILE

if TYPE_CHECKING:
    from .jobs import JobStore

# Oversized historical files are cache misses, never unbounded allocations.
MAX_REUSE_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_REUSE_ARTIFACT_BYTES = 16 * 1024 * 1024
IDENTITY_HASHES = ("model_id_hash", "template_hash", "engine_sampling_hash")


@dataclass(frozen=True)
class ReusedArtifact:
    """Verified text and immutable provenance; no writable source file handle.

    Lookup never publishes into the target manifest or copies/links a file.
    Step 3B callers can record this provenance and use text directly. Subsequent
    lookups reverify disk contents rather than trusting a previously returned path.
    """

    text: str
    name: str
    source_revision: int
    source_config_hash: str
    sha256: str
    dependencies_hash: str
    reused: Literal[True] = True
    generation_budget: int | None = None


@dataclass
class ArtifactReuseCache:
    """Attempt-local verification metadata, never cached artifact text.

    History is bounded by MAX_ARTIFACT_REVISIONS. Parsed manifests use an LRU
    with at most MAX_REUSE_MANIFEST_BYTES of serialized input in total. Source
    and manifest observations are usable only while file metadata still matches;
    every returned artifact is read and hashed again. Never share between runs.
    """

    key: tuple[object, ...] | None = None
    history: ArtifactHistory | None = None
    source: tuple[tuple[int, ...], str] | None = None
    manifests: OrderedDict[str, tuple[tuple[int, ...], dict[str, Any], int]] = field(
        default_factory=OrderedDict
    )
    manifest_bytes: int = 0

    def bind(self, key: tuple[object, ...]) -> None:
        if self.key != key:
            self.key, self.history, self.source = key, None, None
            self.manifests.clear()
            self.manifest_bytes = 0

    def manifest(self, root: int, config_hash: str) -> dict[str, Any]:
        with _regular_file(root, "manifest.json") as stream:
            before = _file_state(stream)
            cached = self.manifests.get(config_hash)
            if cached is not None and cached[0] == before:
                self.manifests.move_to_end(config_hash)
                return cached[1]
            content = _read_stream(stream, MAX_REUSE_MANIFEST_BYTES)
        manifest = json.loads(content, object_pairs_hook=_unique_object)
        if not isinstance(manifest, dict):
            raise ValueError("Unverifiable artifact manifest.")
        if cached is not None:
            self.manifest_bytes -= self.manifests.pop(config_hash)[2]
        while (
            self.manifests
            and self.manifest_bytes + len(content) > MAX_REUSE_MANIFEST_BYTES
        ):
            self.manifest_bytes -= self.manifests.popitem(last=False)[1][2]
        self.manifests[config_hash] = before, manifest, len(content)
        self.manifest_bytes += len(content)
        return manifest


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", name) is None
        or name == "manifest.json"
        or name.endswith(".tmp")
    ):
        raise ValueError("Invalid artifact name.")


@contextmanager
def _directory(path: str | Path, parent: int | None = None) -> Iterator[int]:
    """Anchor each directory component without following symlinks."""
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
    )
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _regular_file(parent: int, name: str) -> Iterator[BinaryIO]:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
    )
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Unverifiable artifact file.")
        yield stream


def _file_state(stream: BinaryIO) -> tuple[int, ...]:
    info = os.fstat(stream.fileno())
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _read_bounded(parent: int, name: str, limit: int) -> bytes:
    with _regular_file(parent, name) as stream:
        return _read_stream(stream, limit)


def _read_stream(stream: BinaryIO, limit: int | None = None) -> bytes:
    """Read once, bounded by the observed size and any historical reuse limit."""
    before = _file_state(stream)
    if limit is not None and before[2] > limit:
        raise ValueError("Artifact exceeds reuse size limit.")
    content = stream.read(before[2] + 1)
    if len(content) != before[2] or before != _file_state(stream):
        raise ValueError("Artifact changed during verification.")
    return content


def _source_hash(
    parent: int,
    cache: ArtifactReuseCache | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> str:
    digest = hashlib.sha256()
    with _regular_file(parent, SOURCE_FILE) as stream:
        before = _file_state(stream)
        if cache is not None and cache.source is not None and cache.source[0] == before:
            return cache.source[1]
        remaining = before[2]
        while remaining:
            if check_cancelled is not None:
                check_cancelled()
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError("Source changed during verification.")
            digest.update(block)
            remaining -= len(block)
        if before != _file_state(stream):
            raise ValueError("Source changed during verification.")
    checksum = digest.hexdigest()
    if cache is not None:
        cache.source = before, checksum
    return checksum


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate manifest key.")
        value[key] = item
    return value


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
        self._bound_identity: dict[str, Any] | None = None
        self.root = self.directory / "artifacts" / self.config_hash
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "manifest.json"
        self.manifest: dict[str, Any]
        with self._root_directory() as root:
            try:
                with _regular_file(root, "manifest.json") as stream:
                    self.manifest = json.loads(_read_stream(stream).decode("utf-8"))
            except FileNotFoundError:
                self.manifest = {
                    "version": 1,
                    "config_hash": self.config_hash,
                    "artifacts": {},
                    "identity": None,
                }
        if (
            self.manifest.get("config_hash") != self.config_hash
            or self.manifest.get("version") != 1
        ):
            raise ProcessingError(
                "artifacts_incompatible",
                "Artifact manifest is incompatible with this job.",
            )

    @contextmanager
    def _root_directory(self) -> Iterator[int]:
        """Anchor reads within this job, refusing linked artifact directories."""
        with (
            _directory(self.directory) as job,
            _directory("artifacts", job) as artifacts,
            _directory(self.config_hash, artifacts) as root,
        ):
            yield root

    def bind_identity(self, identity):
        """Refuse changed or unverifiable identities before reusing existing artifacts."""
        previous = self.manifest.get("identity")
        required = ("model_id_hash", "template_hash", "engine_sampling_hash")
        unverified = bool(self.manifest["artifacts"]) and any(
            (previous or {}).get(key) is None or identity.get(key) is None
            for key in required
        )
        if unverified or (
            previous
            and any(
                previous.get(key) is not None and identity.get(key) != previous[key]
                for key in previous
            )
        ):
            raise ProcessingError(
                "identity_changed",
                "The model or template changed. Start a new analysis.",
            )
        self.manifest["identity"] = identity
        atomic_json(self.path, self.manifest)
        self._bound_identity = copy.deepcopy(identity)

    def read_compatible(
        self,
        name: str,
        dependencies: Mapping[str, object],
        *,
        store: "JobStore",
        job_id: str,
        revision: int,
        max_revisions: int = MAX_ARTIFACT_REVISIONS,
        cache: ArtifactReuseCache | None = None,
        budget_key: str | None = None,
        check_cancelled: Callable[[], None] | None = None,
    ) -> ReusedArtifact | None:
        """Find a completed artifact in this job's compatible older revisions.

        Call bind_identity with a fresh backend observation first, and supply
        the exact dependencies of the newly prepared step (including merge/final
        groups and inputs). No dependency fields are removed or normalized.
        Misses and unsafe/unverifiable historical files return None. Stored
        config corruption and a changed source retain explicit error semantics.
        Ordinary read/write retries are independent of this opt-in lookup.
        An optional attempt-local cache avoids repeated history/PDF/manifest
        reads; contents are always hashed. budget_key retains the source budget
        for generation metrics rather than attributing the target's new budget.
        """
        _validate_name(name)
        if budget_key is not None and budget_key not in RESUME_BUDGETS:
            raise ValueError("Invalid artifact generation budget key.")
        if type(revision) is not int or revision < 0:
            raise ValueError("Invalid job configuration revision.")
        if (
            type(max_revisions) is not int
            or not 1 <= max_revisions <= MAX_ARTIFACT_REVISIONS
        ):
            raise ValueError("Invalid artifact revision lookup limit.")
        if check_cancelled is not None:
            check_cancelled()
        if self.directory.absolute() != store.job_dir(job_id).absolute():
            raise ValueError("Artifact lookup must use the same job directory.")
        if cache is not None:
            cache.bind(
                (
                    store,
                    job_id,
                    revision,
                    max_revisions,
                    self.config_hash,
                    fingerprint(self._bound_identity),
                )
            )
        history = cache.history if cache is not None else None
        if history is None:
            history = store.load_artifact_history(
                job_id, revision=revision, limit=max_revisions
            )
            if cache is not None:
                history["previous"] = [
                    previous
                    for previous in history["previous"]
                    if compatible_artifact_config(
                        previous["snapshot"], history["current"]["snapshot"]
                    )
                ]
                cache.history = history
        if history["current"]["fingerprint"] != self.config_hash:
            raise ProcessingError(
                "artifacts_incompatible",
                "Artifact manifest is incompatible with this job.",
            )
        identity = self._bound_identity
        if (
            identity is None
            or self.manifest.get("identity") != identity
            or not all(_is_hash(identity.get(key)) for key in IDENTITY_HASHES)
        ):
            return None
        if os.open not in os.supports_dir_fd or not all(
            hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
        ):
            return None
        original_source = history["original"].get("source_hash")
        if (
            not _is_hash(original_source)
            or history["current"]["snapshot"].get("source_hash") != original_source
        ):
            return None
        dependencies_hash = fingerprint(dependencies)
        try:
            with (
                _directory(store.data_dir.resolve()) as data,
                _directory("jobs", data) as jobs,
                _directory(job_id, jobs) as job,
            ):
                if _source_hash(job, cache, check_cancelled) != original_source:
                    raise ProcessingError(
                        "source_changed",
                        "The source PDF changed. Start a new analysis.",
                    )
                with _directory("artifacts", job) as artifacts:
                    for previous in history["previous"]:
                        if check_cancelled is not None:
                            check_cancelled()
                        if cache is None and not compatible_artifact_config(
                            previous["snapshot"], history["current"]["snapshot"]
                        ):
                            continue
                        try:
                            with _directory(previous["fingerprint"], artifacts) as root:
                                result = self._read_previous(
                                    root,
                                    name,
                                    dependencies_hash,
                                    identity,
                                    previous["fingerprint"],
                                    previous["revision"],
                                    cache,
                                )
                            if result is not None:
                                if check_cancelled is not None:
                                    check_cancelled()
                                return replace(
                                    result,
                                    generation_budget=cast(
                                        int, previous["snapshot"][budget_key]
                                    )
                                    if budget_key is not None
                                    else None,
                                )
                        except (OSError, ValueError, RecursionError):
                            # A bad revision never legitimizes an unverified file;
                            # an older compatible revision may still be valid.
                            continue
        except (OSError, ValueError):
            return None
        return None

    @staticmethod
    def _read_previous(
        root: int,
        name: str,
        dependencies_hash: str,
        identity: dict[str, Any],
        config_hash: str,
        revision: int,
        cache: ArtifactReuseCache | None = None,
    ) -> ReusedArtifact | None:
        manifest = (
            cache.manifest(root, config_hash)
            if cache is not None
            else json.loads(
                _read_bounded(root, "manifest.json", MAX_REUSE_MANIFEST_BYTES),
                object_pairs_hook=_unique_object,
            )
        )
        if (
            not isinstance(manifest, dict)
            or type(manifest.get("version")) is not int
            or manifest["version"] != 1
            or manifest.get("config_hash") != config_hash
            or fingerprint(manifest.get("identity")) != fingerprint(identity)
            or not isinstance(manifest.get("artifacts"), dict)
        ):
            return None
        entry = manifest["artifacts"].get(name)
        # Version 1 write() publishes only completed results. Unknown completion
        # metadata cannot override that contract (e.g. truncated/cancelled flags).
        if (
            not isinstance(entry, dict)
            or set(entry) != {"complete", "dependencies", "sha256"}
            or entry["complete"] is not True
            or entry["dependencies"] != dependencies_hash
            or not _is_hash(entry["sha256"])
        ):
            return None
        content = _read_bounded(root, name, MAX_REUSE_ARTIFACT_BYTES)
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            return None
        return ReusedArtifact(
            text=content.decode("utf-8"),
            name=name,
            source_revision=revision,
            source_config_hash=config_hash,
            sha256=entry["sha256"],
            dependencies_hash=dependencies_hash,
        )

    def read(self, name, dependencies):
        """Hash and decode the same bytes; unsafe or changed files are cache misses."""
        _validate_name(name)
        entry = self.manifest["artifacts"].get(name)
        if (
            not entry
            or entry.get("complete") is not True
            or entry.get("dependencies") != fingerprint(dependencies)
        ):
            return None
        try:
            with self._root_directory() as root, _regular_file(root, name) as stream:
                content = _read_stream(stream)
        except (OSError, ValueError):
            return None
        if hashlib.sha256(content).hexdigest() != entry.get("sha256"):
            return None
        return content.decode("utf-8")

    def write(self, name, text, dependencies):
        """Publish contents first, then the matching completed manifest entry."""
        path = self.root / name
        atomic_text(path, text)
        self.manifest["artifacts"][name] = {
            "dependencies": fingerprint(dependencies),
            "sha256": file_hash(path),
            "complete": True,
        }
        atomic_json(self.path, self.manifest)
