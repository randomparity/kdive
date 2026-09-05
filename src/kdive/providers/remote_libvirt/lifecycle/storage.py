"""Remote-libvirt overlay volume lifecycle helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.xml import overlay_volume_name, render_volume_xml

_log = logging.getLogger(__name__)

# The provision-time message body for an absent base-image volume (ADR-0080's operator
# prerequisite). Kept as a module constant so the ``ensure_named_overlay`` error has one source;
# the diagnostic ``remote_libvirt_base_image_staging`` check owns its own ``fix`` sentence in
# ``kdive.diagnostics.checks`` (diagnostics → providers is the only legal import direction).
_BASE_VOLUME_NOT_STAGED_MESSAGE = (
    "base image volume {volume!r} is not staged on the remote "
    "host's storage pool (an operator prerequisite, ADR-0080)"
)


class VolumeStaging(StrEnum):
    """Three-state result of a ``lookup_volume_staged`` probe over an open connection.

    ``STAGED`` — the pool exists and the volume is present. ``ABSENT`` — the pool exists but the
    volume is not staged. ``POOL_ABSENT`` — the configured storage pool itself does not exist (a
    different misconfiguration than a missing volume, so callers must not emit a stage-the-volume
    fix for it).
    """

    STAGED = "staged"
    ABSENT = "absent"
    POOL_ABSENT = "pool_absent"


class Volume(Protocol):
    """The storage-volume slice provisioning uses (duck-typed seam)."""

    def path(self) -> str: ...
    def info(self) -> list[int]: ...
    def delete(self, flags: int = 0) -> int: ...


class Pool(Protocol):
    """The storage-pool slice provisioning uses (duck-typed seam)."""

    def storageVolLookupByName(self, name: str) -> Volume: ...  # noqa: N802
    def createXML(self, xml: str, flags: int = 0) -> Volume: ...  # noqa: N802


class _InspectableVolume(Volume, Protocol):
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802


class OverlayPool(Pool, Protocol):
    def storageVolLookupByName(self, name: str) -> _InspectableVolume: ...  # noqa: N802
    def createXML(self, xml: str, flags: int = 0) -> _InspectableVolume: ...  # noqa: N802
    def refresh(self, flags: int = 0) -> int: ...


class StorageConn(Protocol):
    """The connection slice storage lifecycle uses."""

    def storagePoolLookupByName(self, name: str) -> Pool: ...  # noqa: N802


@dataclass(frozen=True, slots=True)
class PreparedOverlay:
    name: str
    backing_path: str
    created: bool


def lookup_pool(conn: StorageConn, pool_name: str) -> Pool:
    """Return a storage pool or map libvirt errors into provider taxonomy."""
    try:
        return conn.storagePoolLookupByName(pool_name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_POOL:
            raise CategorizedError(
                f"storage pool {pool_name!r} does not exist on the remote host",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"pool": pool_name},
            ) from exc
        raise _infra("looking up storage pool", pool=pool_name) from exc


def lookup_volume_staged(conn: StorageConn, pool_name: str, volume_name: str) -> VolumeStaging:
    """Report whether ``volume_name`` is staged in ``pool_name`` over an already-open connection.

    The single "is volume X staged?" path the base-image-staging diagnostic and the
    volume-discoverability read share. It does **not** open or close the connection — the caller
    owns the TLS lifecycle. Only the two clean not-found codes are mapped to a state; any other
    ``libvirtError`` (a transport drop mid-RPC, an internal error) is **re-raised** so the caller
    decides how to classify it rather than this helper collapsing an infra fault into a clean
    ``STAGED``/``ABSENT`` verdict.

    Args:
        conn: An open libvirt connection exposing ``storagePoolLookupByName``.
        pool_name: The storage pool to look the volume up in.
        volume_name: The base-image volume name.

    Returns:
        ``STAGED`` if the volume is present, ``ABSENT`` if the pool exists but the volume does not,
        ``POOL_ABSENT`` if the pool itself does not exist.

    Raises:
        libvirt.libvirtError: Any error other than ``VIR_ERR_NO_STORAGE_POOL`` /
            ``VIR_ERR_NO_STORAGE_VOL`` (re-raised unchanged).
    """
    try:
        pool = conn.storagePoolLookupByName(pool_name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_POOL:
            return VolumeStaging.POOL_ABSENT
        raise
    try:
        pool.storageVolLookupByName(volume_name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
            return VolumeStaging.ABSENT
        raise
    return VolumeStaging.STAGED


def ensure_overlay(pool: OverlayPool, base_volume: str, system_id: UUID) -> PreparedOverlay:
    """Create the per-System overlay volume when absent; reuse it when present."""
    return ensure_named_overlay(pool, base_volume, overlay_volume_name(system_id))


def ensure_named_overlay(pool: OverlayPool, base_volume: str, name: str) -> PreparedOverlay:
    """Create the named overlay volume over ``base_volume`` when absent; reuse it when present.

    The volume name is supplied by the caller so a build VM can use an overlay name disjoint
    from the per-System scheme (ADR-0100); :func:`ensure_overlay` is the System-scheme wrapper.
    """
    try:
        pool.refresh()
    except libvirt.libvirtError as exc:
        raise _infra("refreshing storage pool") from exc
    try:
        base = pool.storageVolLookupByName(base_volume)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
            raise CategorizedError(
                _BASE_VOLUME_NOT_STAGED_MESSAGE.format(volume=base_volume),
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"base_image_volume": base_volume},
            ) from exc
        raise _infra("looking up base image volume", volume=base_volume) from exc
    try:
        backing_path = base.path()
    except libvirt.libvirtError as exc:
        raise _infra("resolving base image volume path", volume=base_volume) from exc
    base_xml = _require_backing_path(base, expected=None, volume=base_volume)
    from kdive.providers.remote_libvirt.lifecycle.xml import volume_target_identity

    try:
        owner_id, group_id = volume_target_identity(base_xml)
    except ValueError as exc:
        raise CategorizedError(
            "remote base volume has invalid target ownership metadata",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"volume": base_volume},
        ) from exc
    if _volume_exists(pool, name):
        try:
            overlay = pool.storageVolLookupByName(name)
        except libvirt.libvirtError as exc:
            raise _infra("reading existing overlay volume", volume=name) from exc
        _require_backing_path(overlay, expected=backing_path, volume=name)
        return PreparedOverlay(name=name, backing_path=backing_path, created=False)
    created: Volume | None = None
    try:
        capacity = int(base.info()[1])
        xml = render_volume_xml(
            name,
            capacity_bytes=capacity,
            backing_path=backing_path,
            owner_id=owner_id,
            group_id=group_id,
        )
        created = pool.createXML(xml)
    except libvirt.libvirtError as exc:
        raise CategorizedError(
            "could not create the per-System overlay volume",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"volume": name},
        ) from exc
    try:
        _require_backing_path(created, expected=backing_path, volume=name)
    except CategorizedError:
        try:
            created.delete()
        except libvirt.libvirtError:
            _log.warning("failed to remove invalid newly created overlay volume %s", name)
        raise
    return PreparedOverlay(name=name, backing_path=backing_path, created=True)


def _require_backing_path(item: _InspectableVolume, *, expected: str | None, volume: str) -> str:
    """Require terminal bases or an overlay's exact immediate backing path."""
    from kdive.providers.remote_libvirt.lifecycle.xml import volume_backing_path

    try:
        volume_xml = item.XMLDesc()
    except libvirt.libvirtError as exc:
        raise _infra("reading storage volume metadata", volume=volume) from exc
    try:
        actual = volume_backing_path(volume_xml)
    except ValueError as exc:
        raise CategorizedError(
            "remote storage volume has invalid backing metadata",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"volume": volume},
        ) from exc
    if actual != expected:
        raise CategorizedError(
            "remote storage volume has unexpected backing metadata",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"volume": volume},
        )
    return volume_xml


def cleanup_overlay_if_created(pool: Pool, overlay: PreparedOverlay) -> None:
    """Reclaim an overlay this attempt created; never one a running System owns."""
    if not overlay.created:
        return
    try:
        pool.storageVolLookupByName(overlay.name).delete()
    except libvirt.libvirtError:
        _log.warning("failed to remove overlay volume %s after failed provision", overlay.name)


def cleanup_created_volume(pool: Pool, name: str, *, created: bool) -> None:
    """Best-effort reclaim a base volume this attempt staged; never a pre-existing one (ADR-0440).

    Mirrors :func:`cleanup_overlay_if_created`: an operator-staged base (or a base a prior attempt
    already staged and this one reused) has ``created=False`` and is left in place; a libvirt fault
    is swallowed so a reclaim never masks the original provisioning error.
    """
    if not created:
        return
    try:
        pool.storageVolLookupByName(name).delete()
    except libvirt.libvirtError:
        _log.warning("failed to remove staged base volume %s after failed provision", name)


def delete_volume(conn: StorageConn, pool_name: str, volume_name: str) -> None:
    """Delete an overlay volume; absent pool/volume are achieved post-states."""
    try:
        pool = conn.storagePoolLookupByName(pool_name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_POOL:
            return
        raise _infra("looking up storage pool", pool=pool_name) from exc
    try:
        volume = pool.storageVolLookupByName(volume_name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
            return
        raise _infra("looking up overlay volume", volume=volume_name) from exc
    try:
        volume.delete()
    except libvirt.libvirtError as exc:
        raise _infra("deleting overlay volume", volume=volume_name) from exc


def _volume_exists(pool: Pool, name: str) -> bool:
    try:
        pool.storageVolLookupByName(name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
            return False
        raise _infra("looking up overlay volume", volume=name) from exc
    return True


def _infra(verb: str, **details: str) -> CategorizedError:
    return CategorizedError(
        f"libvirt error {verb}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details=dict(details),
    )
