"""Upload a local qcow2 into a remote-libvirt storage pool as a base-image volume (ADR-0336).

The net-new counterpart to the volume *download* the host-dump capture uses: kdive has always let
operators stage a base-image volume out of band, so there was no upload primitive. ``stage-volume``
needs one to place a KDIVE-built qcow2 on the remote host in the same step it captures the image's
kernel config. The libvirt sequence — create the volume, open a stream, ``upload`` + ``sendAll`` the
bytes, ``finish`` — runs over the caller's already-open mutual-TLS connection; a fault cleans up the
partially-created volume before surfacing an infrastructure failure.

The connection/pool/volume/stream slices are duck-typed protocols so the orchestration is
unit-tested with fakes; the real ``libvirt`` objects satisfy them structurally.
"""

from __future__ import annotations

import contextlib
import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory

_log = logging.getLogger(__name__)

#: The libvirt ``sendAll`` source callback: ``(stream, nbytes, opaque) -> bytes``.
StreamSource = Callable[[object, int, object], bytes]

#: The qcow2 magic every staged base volume must start with (bytes ``51 46 49 fb``); a file that
#: does not is rejected before create+stream rather than landing an invalid volume that fails late
#: and confusingly at boot (ADR-0440, matching local ADR-0434/0438).
_QCOW2_MAGIC = b"QFI\xfb"


class _UploadStream(Protocol):
    def sendAll(self, handler: StreamSource, opaque: object) -> None: ...  # noqa: N802
    def finish(self) -> int: ...
    def abort(self) -> int: ...


class _UploadVolume(Protocol):
    def upload(self, stream: object, offset: int, length: int, flags: int = 0) -> int: ...
    def delete(self, flags: int = 0) -> int: ...


class _UploadPool(Protocol):
    def createXML(self, xml: str, flags: int = 0) -> _UploadVolume: ...  # noqa: N802
    def storageVolLookupByName(self, name: str) -> _UploadVolume: ...  # noqa: N802


class VolumeUploadConn(Protocol):
    """The libvirt connection slice the volume upload uses."""

    def storagePoolLookupByName(self, name: str) -> _UploadPool: ...  # noqa: N802
    def newStream(self, flags: int = 0) -> _UploadStream: ...  # noqa: N802


def render_base_volume_xml(name: str, *, capacity_bytes: int) -> str:
    """Render a standalone qcow2 volume (no backing store), sized to ``capacity_bytes``.

    Unlike the overlay XML (which backs onto a base volume), a staged base image is a full,
    independent qcow2, so it carries a ``target`` format but no ``backingStore``.
    """
    volume = ET.Element("volume")
    ET.SubElement(volume, "name").text = name
    ET.SubElement(volume, "capacity").text = str(capacity_bytes)
    target = ET.SubElement(volume, "target")
    ET.SubElement(target, "format", type="qcow2")
    return ET.tostring(volume, encoding="unicode")


def _require_qcow2_magic(qcow2_path: Path) -> None:
    """Reject a supplied base image that does not start with the qcow2 magic (ADR-0440).

    Reads only the first four bytes. A format-invalid file is a caller error, so it raises
    ``CONFIGURATION_ERROR`` — not the ``INFRASTRUCTURE_FAILURE`` reserved for libvirt faults.
    """
    try:
        with qcow2_path.open("rb") as handle:
            head = handle.read(4)
    except OSError as exc:
        raise CategorizedError(
            f"could not read the supplied base image at {str(qcow2_path)!r}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"path": str(qcow2_path)},
        ) from exc
    if head != _QCOW2_MAGIC:
        raise CategorizedError(
            "the supplied base image is not a qcow2 file (its bytes do not start with the qcow2 "
            "magic); stage a qcow2 image",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"path": str(qcow2_path)},
        )


def _infra(operation: str, **details: str) -> CategorizedError:
    payload: dict[str, object] = dict(details)
    return CategorizedError(
        f"remote-libvirt volume upload failed while {operation}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details=payload,
    )


def upload_qcow2_volume(
    conn: VolumeUploadConn, pool_name: str, volume_name: str, qcow2_path: Path
) -> None:
    """Create ``volume_name`` in ``pool_name`` and stream ``qcow2_path`` into it.

    Fatal on any libvirt fault: the volume must land, so a create/upload failure raises an
    ``INFRASTRUCTURE_FAILURE`` after aborting the stream and deleting the partially-created volume
    (best-effort cleanup, so a stale zero-byte volume never shadows a later retry).

    Args:
        conn: An open mutual-TLS libvirt connection (the caller owns its lifecycle).
        pool_name: The storage pool to create the volume in.
        volume_name: The base-image volume name to create.
        qcow2_path: The local built qcow2 to upload.

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` on any libvirt error during lookup, create, or
            stream.
    """
    capacity = qcow2_path.stat().st_size
    try:
        pool = conn.storagePoolLookupByName(pool_name)
    except libvirt.libvirtError as exc:
        raise _infra("looking up the storage pool", pool=pool_name) from exc
    # Idempotent re-run: a volume already present is a prior successful upload (a failed upload
    # deletes its partial volume), so skip the create+stream and let the caller re-attach the
    # config. Only a genuinely-absent volume is created and streamed.
    try:
        pool.storageVolLookupByName(volume_name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() != libvirt.VIR_ERR_NO_STORAGE_VOL:
            raise _infra("looking up the volume", pool=pool_name, volume=volume_name) from exc
    else:
        _log.info("volume %s already staged in pool %s; skipping upload", volume_name, pool_name)
        return
    _require_qcow2_magic(qcow2_path)
    try:
        volume = pool.createXML(render_base_volume_xml(volume_name, capacity_bytes=capacity))
    except libvirt.libvirtError as exc:
        raise _infra("creating the volume", pool=pool_name, volume=volume_name) from exc
    stream = conn.newStream(0)
    try:
        volume.upload(stream, 0, capacity, 0)
        with qcow2_path.open("rb") as handle:
            stream.sendAll(lambda _stream, nbytes, _opaque: handle.read(nbytes), None)
        stream.finish()
    except libvirt.libvirtError as exc:
        with contextlib.suppress(libvirt.libvirtError):
            stream.abort()
        with contextlib.suppress(libvirt.libvirtError):
            volume.delete(0)
        raise _infra("streaming the qcow2", pool=pool_name, volume=volume_name) from exc
    _log.info("uploaded %s to remote pool %s as volume %s", qcow2_path, pool_name, volume_name)
