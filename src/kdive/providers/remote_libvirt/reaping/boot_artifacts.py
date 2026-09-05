"""Remote external-boot artifact ownership and orphan cleanup (ADR-0583, ADR-0599)."""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable, Collection
from typing import Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.boot_artifact_name import (
    BootArtifactKind,
    BootArtifactName,
    parse_boot_artifact_name,
)

BootArtifactVolume = BootArtifactName


class _Volume(Protocol):
    def name(self) -> str: ...
    def delete(self, flags: int = 0) -> int: ...
    def download(self, stream: object, offset: int, length: int, flags: int = 0) -> int: ...


class _Stream(Protocol):
    def recvAll(
        self, callback: Callable[[object, bytes, object], None], opaque: object
    ) -> None: ...  # noqa: N802
    def finish(self) -> int: ...
    def abort(self) -> int: ...


class _Pool(Protocol):
    def refresh(self, flags: int = 0) -> int: ...
    def listAllVolumes(self, flags: int = 0) -> list[_Volume]: ...  # noqa: N802


class BootArtifactReaperConn(Protocol):
    def storagePoolLookupByName(self, name: str) -> _Pool: ...  # noqa: N802
    def newStream(self, flags: int = 0) -> _Stream: ...  # noqa: N802


def _parse(volume: _Volume) -> BootArtifactVolume | None:
    return parse_boot_artifact_name(volume.name())


def list_owned_boot_artifacts(
    conn: BootArtifactReaperConn, pool_name: str
) -> list[BootArtifactVolume]:
    """List only canonical KDIVE names whose complete bytes match their digest."""
    try:
        pool = conn.storagePoolLookupByName(pool_name)
        pool.refresh(0)
        artifacts: list[BootArtifactVolume] = []
        for volume in pool.listAllVolumes(0):
            artifact = _parse(volume)
            if artifact is not None and _content_matches(conn, volume, artifact.digest):
                artifacts.append(artifact)
        return artifacts
    except libvirt.libvirtError as exc:
        raise CategorizedError(
            "could not enumerate remote boot-artifact storage",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"pool": pool_name},
        ) from exc


def reap_orphaned_boot_artifacts(
    conn: BootArtifactReaperConn,
    pool_name: str,
    *,
    live_owners: Collection[tuple[BootArtifactKind, UUID, UUID, str]],
) -> int:
    """Delete name-verified artifacts not present in the durable live-owner set.

    A malformed, foreign, or digest-mismatched volume is never considered a candidate. Deletion is
    idempotent: an already-gone volume is treated as an achieved post-state by the next sweep.
    """
    try:
        pool = conn.storagePoolLookupByName(pool_name)
        pool.refresh(0)
    except libvirt.libvirtError as exc:
        raise CategorizedError(
            "could not open remote boot-artifact storage for reaping",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"pool": pool_name},
        ) from exc
    try:
        volumes = pool.listAllVolumes(0)
        removed = 0
        live = set(live_owners)
        for volume in volumes:
            artifact = _parse(volume)
            if (
                artifact is None
                or artifact.owner in live
                or not _content_matches(conn, volume, artifact.digest)
            ):
                continue
            try:
                volume.delete(0)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() != libvirt.VIR_ERR_NO_STORAGE_VOL:
                    raise CategorizedError(
                        "could not delete orphaned remote boot artifact",
                        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                        details={"pool": pool_name, "volume": artifact.name},
                    ) from exc
            removed += 1
        return removed
    except libvirt.libvirtError as exc:
        raise CategorizedError(
            "could not enumerate remote boot-artifact storage for reaping",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"pool": pool_name},
        ) from exc


def _content_matches(conn: BootArtifactReaperConn, volume: _Volume, expected: str) -> bool:
    """Confirm the volume's complete bytes before considering it owned and removable."""
    stream = conn.newStream(0)
    digest = hashlib.sha256()

    def receive(_stream: object, chunk: bytes, _opaque: object) -> None:
        digest.update(chunk)

    try:
        volume.download(stream, 0, 0, 0)
        stream.recvAll(receive, None)
        stream.finish()
    except libvirt.libvirtError, OSError, RuntimeError, AttributeError:
        with contextlib.suppress(libvirt.libvirtError, AttributeError):
            stream.abort()
        return False
    return digest.hexdigest() == expected.removeprefix("sha256:")


__all__ = [
    "BootArtifactReaperConn",
    "BootArtifactVolume",
    "list_owned_boot_artifacts",
    "reap_orphaned_boot_artifacts",
]
