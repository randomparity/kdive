"""Stage the plan's uploaded vmlinux as a member of the remote module source tree (#3131).

The module appliance's only mutable destination is ``lib/modules/<release>/``, so the file
travels in the module source tree and lands at ``lib/modules/<release>/vmlinux``. The tree's
manifest therefore names it: this stager swaps in the plan's combined manifest and, when it
fetches, leaves the verified bytes beside the module archive as ``debuginfo``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from typing import BinaryIO

from kdive.providers.ports.external_boot import (
    DebuginfoSource,
    ExternalBootArtifactStager,
    ExternalBootPlan,
)
from kdive.store.objectstore import ObjectStore

STAGED_NAME = "debuginfo"
_NEXT = ".debuginfo.next"
_CHUNK_BYTES = 1024 * 1024
_DOMAIN = b"kdive-remote-installed-with-debuginfo-v1\0"


def installed_tree_identity(base: str, source: DebuginfoSource) -> str:
    """Identity of the installed tree: the module tree's plus the staged file's."""
    payload = b"\0".join((base.encode(), source.sha256.encode(), str(source.size_bytes).encode()))
    return "sha256:" + hashlib.sha256(_DOMAIN + payload).hexdigest()


class DebuginfoModuleTreeStager:
    """Wrap an authority-host stager so a plan's vmlinux joins the module source tree."""

    def __init__(
        self, inner: ExternalBootArtifactStager, object_store: ObjectStore, *, fetch: bool
    ) -> None:
        self._inner = inner
        self._object_store = object_store
        self._fetch = fetch

    def materialize_artifacts(
        self, plan: ExternalBootPlan, directory_fd: int
    ) -> tuple[dict[str, object], str]:
        evidence, installed = self._inner.materialize_artifacts(plan, directory_fd)
        source = plan.module_tree_debuginfo
        if source is None:
            return evidence, installed
        if self._fetch:
            self._stage(source, directory_fd)
        return (
            {**evidence, "module_source_manifest": plan.module_tree_manifest},
            installed_tree_identity(installed, source),
        )

    def _stage(self, source: DebuginfoSource, directory_fd: int) -> None:
        if _matches_existing(directory_fd, source):
            return
        with contextlib.suppress(FileNotFoundError):
            os.unlink(_NEXT, dir_fd=directory_fd)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(_NEXT, flags, 0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(descriptor, "wb") as staged:
                digest, size = self._copy_exact_version(source, staged)
                staged.flush()
                os.fsync(staged.fileno())
            if "sha256:" + digest != source.sha256 or size != source.size_bytes:
                raise ValueError("exact vmlinux version does not match the external-boot plan")
            os.rename(_NEXT, STAGED_NAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(_NEXT, dir_fd=directory_fd)
            raise

    def _copy_exact_version(
        self, source: DebuginfoSource, destination: BinaryIO
    ) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with self._object_store.get_artifact_stream(
            source.key, None, version_id=source.version
        ) as streamed:
            while chunk := streamed.reader.read(_CHUNK_BYTES):
                size += len(chunk)
                if size > source.size_bytes:
                    raise ValueError("exact vmlinux version exceeds the external-boot plan size")
                digest.update(chunk)
                destination.write(chunk)
        return digest.hexdigest(), size


def _matches_existing(directory_fd: int, source: DebuginfoSource) -> bool:
    """True when a verified ``debuginfo`` is already present; refuse one that differs."""
    try:
        descriptor = os.open(STAGED_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        return False
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(descriptor, "rb") as existing:
        while chunk := existing.read(_CHUNK_BYTES):
            size += len(chunk)
            digest.update(chunk)
    if "sha256:" + digest.hexdigest() != source.sha256 or size != source.size_bytes:
        raise ValueError("staged vmlinux does not match the external-boot plan")
    return True
