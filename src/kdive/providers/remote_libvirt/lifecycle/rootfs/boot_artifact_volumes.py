"""Remote-libvirt external-boot kernel and initrd volumes (ADR-0583).

The connection passed to this module is already an authenticated libvirt connection.  This module
owns only the bounded storage-volume transfer: names and references are deterministic from the
System/Run ownership, a completed volume is published only after the stream is finished, and a
failed attempt removes the volume it created.  It deliberately does not edit modules or activate
the domain.
"""

from __future__ import annotations

import contextlib
import hashlib
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.external_boot import OpaqueProviderRef

type BootArtifactKind = Literal["kernel", "initrd"]


class _ArtifactStream(Protocol):
    def sendAll(self, callback: Callable[[object, int, object], bytes], opaque: object) -> None: ...  # noqa: N802
    def recvAll(
        self, callback: Callable[[object, bytes, object], None], opaque: object
    ) -> None: ...  # noqa: N802
    def finish(self) -> int: ...
    def abort(self) -> int: ...


class _ArtifactVolume(Protocol):
    def upload(self, stream: object, offset: int, length: int, flags: int = 0) -> int: ...
    def download(self, stream: object, offset: int, length: int, flags: int = 0) -> int: ...
    def delete(self, flags: int = 0) -> int: ...


class _ArtifactPool(Protocol):
    def storageVolLookupByName(self, name: str) -> _ArtifactVolume: ...  # noqa: N802
    def createXML(self, xml: str, flags: int = 0) -> _ArtifactVolume: ...  # noqa: N802
    def createXMLFrom(
        self, xml: str, volume: _ArtifactVolume, flags: int = 0
    ) -> _ArtifactVolume: ...  # noqa: N802


class BootArtifactVolumeConn(Protocol):
    def storagePoolLookupByName(self, name: str) -> _ArtifactPool: ...  # noqa: N802
    def newStream(self, flags: int = 0) -> _ArtifactStream: ...  # noqa: N802


@dataclass(frozen=True, slots=True)
class MaterializedBootArtifacts:
    """Opaque references to the provider-local kernel and optional initrd volumes."""

    kernel: OpaqueProviderRef
    initrd: OpaqueProviderRef | None


@dataclass(frozen=True, slots=True)
class BootArtifact:
    """One exact artifact payload and its durable System/Run ownership."""

    kind: BootArtifactKind
    system_id: UUID
    run_id: UUID
    payload: bytes


def artifact_volume_name(kind: BootArtifactKind, system_id: UUID, run_id: UUID) -> str:
    """Return the deterministic final volume name for one owned boot artifact."""
    return f"kdive-{kind}-{system_id}-{run_id}"


def artifact_partial_volume_name(
    kind: BootArtifactKind, system_id: UUID, run_id: UUID, payload: bytes, attempt_id: UUID
) -> str:
    """Return the attempt-owned staging volume name."""
    digest = hashlib.sha256(payload).hexdigest()
    return f"{artifact_volume_name(kind, system_id, run_id)}-partial-{attempt_id}-{digest}"


def artifact_volume_ref(kind: BootArtifactKind, system_id: UUID, run_id: UUID) -> OpaqueProviderRef:
    """Return the opaque provider reference for one deterministic final volume."""
    return OpaqueProviderRef(ref=f"{kind}/{system_id}/{run_id}")


def render_boot_artifact_volume_xml(name: str, *, capacity_bytes: int) -> str:
    """Render an independent raw volume whose capacity is the exact transferred byte count."""
    volume = ET.Element("volume")
    ET.SubElement(volume, "name").text = name
    ET.SubElement(volume, "capacity").text = str(capacity_bytes)
    target = ET.SubElement(volume, "target")
    ET.SubElement(target, "format", type="raw")
    return ET.tostring(volume, encoding="unicode")


def _infra(operation: str, *, kind: BootArtifactKind, pool: str) -> CategorizedError:
    return CategorizedError(
        f"remote-libvirt boot artifact transfer failed while {operation}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"kind": kind, "pool": pool},
    )


def _conflict(kind: BootArtifactKind, pool: str) -> CategorizedError:
    return CategorizedError(
        "remote-libvirt boot artifact volume has different bytes for the same ownership identity",
        category=ErrorCategory.CONFLICT,
        details={"kind": kind, "pool": pool},
    )


def _lookup(pool: _ArtifactPool, name: str) -> _ArtifactVolume | None:
    try:
        return pool.storageVolLookupByName(name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
            return None
        raise


def _rehash_volume(conn: BootArtifactVolumeConn, volume: _ArtifactVolume, expected: bytes) -> bool:
    """Hash the remote bytes through libvirt without buffering a second artifact copy."""
    stream = conn.newStream(0)
    digest = hashlib.sha256()
    size = 0

    def receive(_stream: object, chunk: bytes, _opaque: object) -> None:
        nonlocal size
        digest.update(chunk)
        size += len(chunk)

    try:
        volume.download(stream, 0, 0, 0)
        stream.recvAll(receive, None)
        stream.finish()
    except libvirt.libvirtError, OSError, RuntimeError:
        with contextlib.suppress(libvirt.libvirtError):
            stream.abort()
        raise
    return size == len(expected) and digest.digest() == hashlib.sha256(expected).digest()


def _upload_volume(
    conn: BootArtifactVolumeConn,
    pool: _ArtifactPool,
    name: str,
    payload: bytes,
    *,
    kind: BootArtifactKind,
    pool_name: str,
) -> _ArtifactVolume:
    volume: _ArtifactVolume | None = None
    stream: _ArtifactStream | None = None
    try:
        volume = pool.createXML(render_boot_artifact_volume_xml(name, capacity_bytes=len(payload)))
        stream = conn.newStream(0)
        volume.upload(stream, 0, len(payload), 0)
        sent = hashlib.sha256()
        position = 0

        def send(_stream: object, nbytes: int, _opaque: object) -> bytes:
            nonlocal position
            chunk = payload[position : position + nbytes]
            position += len(chunk)
            sent.update(chunk)
            return chunk

        stream.sendAll(send, None)
        stream.finish()
        if position != len(payload) or sent.digest() != hashlib.sha256(payload).digest():
            raise ValueError("libvirt stream did not transfer the exact artifact bytes")
    except (libvirt.libvirtError, OSError, ValueError) as exc:
        if stream is not None:
            with contextlib.suppress(libvirt.libvirtError):
                stream.abort()
        if volume is not None:
            with contextlib.suppress(libvirt.libvirtError):
                volume.delete(0)
        raise _infra("streaming the artifact", kind=kind, pool=pool_name) from exc
    assert volume is not None
    return volume


def _materialize_one(
    conn: BootArtifactVolumeConn,
    pool_name: str,
    system_id: UUID,
    run_id: UUID,
    kind: BootArtifactKind,
    payload: bytes,
    attempt_id: UUID,
) -> OpaqueProviderRef:
    name = artifact_volume_name(kind, system_id, run_id)
    ref = artifact_volume_ref(kind, system_id, run_id)
    try:
        pool = conn.storagePoolLookupByName(pool_name)
        existing = _lookup(pool, name)
    except libvirt.libvirtError as exc:
        raise _infra("looking up the artifact volume", kind=kind, pool=pool_name) from exc
    if existing is not None:
        try:
            matches = _rehash_volume(conn, existing, payload)
        except (libvirt.libvirtError, OSError, RuntimeError) as exc:
            raise _infra("rehashing the existing artifact", kind=kind, pool=pool_name) from exc
        if not matches:
            raise _conflict(kind, pool_name)
        return ref
    partial_name = artifact_partial_volume_name(kind, system_id, run_id, payload, attempt_id)
    staged: _ArtifactVolume | None = None
    try:
        partial = _lookup(pool, partial_name)
        if partial is not None:
            # A partial with this deterministic ownership key can only be from an earlier attempt
            # of this exact artifact.  It is never a published volume and is safe to replace.
            partial.delete(0)
        staged = _upload_volume(conn, pool, partial_name, payload, kind=kind, pool_name=pool_name)
        if not _rehash_volume(conn, staged, payload):
            raise _infra("verifying the staged artifact", kind=kind, pool=pool_name)
        pool.createXMLFrom(
            render_boot_artifact_volume_xml(name, capacity_bytes=len(payload)), staged
        )
        try:
            staged.delete(0)
        except libvirt.libvirtError as exc:
            # The final copy is complete and immutable; leave it available for an identity-checking
            # retry while surfacing the cleanup fault as infrastructure failure.
            raise _infra("cleaning the staged artifact", kind=kind, pool=pool_name) from exc
    except CategorizedError:
        if staged is not None:
            with contextlib.suppress(libvirt.libvirtError):
                staged.delete(0)
        raise
    except (libvirt.libvirtError, OSError, RuntimeError) as exc:
        if staged is not None:
            with contextlib.suppress(libvirt.libvirtError):
                staged.delete(0)
        raise _infra("publishing the artifact", kind=kind, pool=pool_name) from exc
    return ref


def create_boot_artifact_volume(
    conn: BootArtifactVolumeConn, pool_name: str, artifact: BootArtifact
) -> OpaqueProviderRef:
    """Create or identity-check one deterministic artifact volume."""
    return _materialize_one(
        conn,
        pool_name,
        artifact.system_id,
        artifact.run_id,
        artifact.kind,
        bytes(artifact.payload),
        uuid4(),
    )


def _read_payload(value: bytes | bytearray | memoryview | Path) -> bytes:
    if isinstance(value, Path):
        try:
            return value.read_bytes()
        except OSError as exc:
            raise CategorizedError(
                "could not read the local boot artifact",
                category=ErrorCategory.CONFIGURATION_ERROR,
            ) from exc
    return bytes(value)


def materialize_boot_artifacts(
    conn: BootArtifactVolumeConn,
    pool_name: str,
    *,
    system_id: UUID,
    run_id: UUID,
    kernel: bytes | bytearray | memoryview | Path,
    initrd: bytes | bytearray | memoryview | Path | None,
    attempt_id: UUID | None = None,
) -> MaterializedBootArtifacts:
    """Transfer exact kernel and optional initrd bytes over the existing mTLS connection.

    A deterministic final volume is returned only after ``finish`` succeeds.  An existing final
    volume is reused only when its bytes rehash to the requested payload; otherwise a non-retryable
    conflict is raised and the existing volume is left untouched.  Any volume created by this
    attempt is deleted on transfer failure.
    """
    kernel_payload = _read_payload(kernel)
    if not kernel_payload:
        raise CategorizedError(
            "kernel boot artifact must not be empty",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    attempt = attempt_id or uuid4()
    kernel_ref = _materialize_one(
        conn, pool_name, system_id, run_id, "kernel", kernel_payload, attempt
    )
    initrd_ref = (
        _materialize_one(
            conn, pool_name, system_id, run_id, "initrd", _read_payload(initrd), attempt
        )
        if initrd is not None
        else None
    )
    return MaterializedBootArtifacts(kernel=kernel_ref, initrd=initrd_ref)
