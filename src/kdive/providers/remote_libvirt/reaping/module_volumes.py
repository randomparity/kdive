"""Attachment-safe remote module volume ownership and reaping (ADR-0588, ADR-0603)."""

from __future__ import annotations

import logging
import posixpath
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection, Sequence
from typing import Protocol

import libvirt
from defusedxml.common import DefusedXmlException

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    RemoteDeviceIdentity,
    RemoteDeviceIdentityPort,
    path_references,
    volume_references,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volume_names import (
    ModuleVolumeOwner,
    parse_module_volume_name,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.xml_bounds import XmlEnumerationBudget

_MAX_STORAGE_PATH_BYTES = 4096
_MAX_IDENTITY_COMPONENT = (1 << 64) - 1
_MAX_PATH_IDENTITIES = 4096
_LOG = logging.getLogger(__name__)


class _Volume(Protocol):
    def name(self) -> str: ...
    def path(self) -> str: ...
    def delete(self, flags: int = 0) -> int: ...


class _Pool(Protocol):
    def refresh(self, flags: int = 0) -> int: ...
    def listAllVolumes(self, flags: int = 0) -> list[_Volume]: ...  # noqa: N802
    def storageVolLookupByName(self, name: str) -> _Volume: ...  # noqa: N802


class _Domain(Protocol):
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802
    def isActive(self) -> int: ...  # noqa: N802
    def isPersistent(self) -> int: ...  # noqa: N802


class ModuleVolumeReaperConn(Protocol):
    def storagePoolLookupByName(self, name: str) -> _Pool: ...  # noqa: N802
    def listAllDomains(self, flags: int = 0) -> Sequence[_Domain]: ...  # noqa: N802


def _infra(message: str, **details: object) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.INFRASTRUCTURE_FAILURE, details=details)


def _conflict(message: str, **details: object) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFLICT, details=details)


def _open_pool(conn: ModuleVolumeReaperConn, pool_name: str) -> _Pool:
    try:
        pool = conn.storagePoolLookupByName(pool_name)
        pool.refresh(0)
        return pool
    except libvirt.libvirtError as exc:
        raise _infra("could not open remote module storage", pool=pool_name) from exc


def _enumerate_owned(pool: _Pool, pool_name: str) -> list[tuple[_Volume, ModuleVolumeOwner]]:
    try:
        volumes = pool.listAllVolumes(0)
        return [
            (volume, owner)
            for volume in volumes
            if (owner := parse_module_volume_name(volume.name())) is not None
        ]
    except libvirt.libvirtError as exc:
        raise _infra("could not enumerate remote module storage", pool=pool_name) from exc


def list_owned_module_volumes(
    conn: ModuleVolumeReaperConn, pool_name: str
) -> list[tuple[str, ModuleVolumeOwner]]:
    """List complete-name-owned module volumes without reading their contents."""
    pool = _open_pool(conn, pool_name)
    return [(volume.name(), owner) for volume, owner in _enumerate_owned(pool, pool_name)]


def _normalize(path: str, **details: object) -> str:
    if (
        type(path) is not str
        or not path.startswith("/")
        or "\x00" in path
        or len(path.encode()) > _MAX_STORAGE_PATH_BYTES
    ):
        raise _conflict("remote module storage path is invalid", **details)
    return posixpath.normpath("/" + path.lstrip("/"))


def _domain_documents(domain: _Domain) -> list[str]:
    try:
        active = bool(domain.isActive())
        documents = [domain.XMLDesc(0)]
        if active and domain.isPersistent():
            documents.append(domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
        return documents
    except libvirt.libvirtError as exc:
        raise _infra("could not read remote module attachment") from exc


def _reference_paths(conn: ModuleVolumeReaperConn) -> set[str]:
    try:
        domains = conn.listAllDomains(0)
    except libvirt.libvirtError as exc:
        raise _infra("could not enumerate remote module attachments") from exc
    result: set[str] = set()
    budget = XmlEnumerationBudget()
    for domain in domains:
        for document in _domain_documents(domain):
            try:
                root = budget.parse(document)
            except (ET.ParseError, DefusedXmlException, ValueError) as exc:
                raise _conflict("could not inspect remote module attachment") from exc
            for path in path_references(root):
                result.add(_normalize(path))
            for pool_name, volume_name in volume_references(root):
                try:
                    pool = conn.storagePoolLookupByName(pool_name)
                    path = pool.storageVolLookupByName(volume_name).path()
                except libvirt.libvirtError as exc:
                    raise _infra(
                        "could not resolve remote module attachment",
                        pool=pool_name,
                        volume=volume_name,
                    ) from exc
                result.add(_normalize(path, pool=pool_name, volume=volume_name))
    return result


def referenced_volume_paths(conn: ModuleVolumeReaperConn) -> frozenset[str]:
    """Return normalized paths from every active and inactive domain disk graph."""
    return frozenset(_reference_paths(conn))


def _identity(
    port: RemoteDeviceIdentityPort, path: str, *, pool: str, volume: str | None = None
) -> RemoteDeviceIdentity:
    details: dict[str, object] = {"pool": pool}
    if volume is not None:
        details["volume"] = volume
    try:
        identity = port.identity(path)
    except Exception:
        raise _infra("remote device identity lookup failed", **details) from None
    if identity is None:
        raise _conflict("remote device identity is unavailable", **details)
    if (
        type(identity) is not RemoteDeviceIdentity
        or identity.kind not in {"inode", "block"}
        or type(identity.primary) is not int
        or type(identity.secondary) is not int
        or not 0 <= identity.primary <= _MAX_IDENTITY_COMPONENT
        or not 0 <= identity.secondary <= _MAX_IDENTITY_COMPONENT
        or (identity.kind == "block" and identity.secondary != 0)
    ):
        raise _conflict("remote device identity is invalid", **details)
    return identity


def _attempt(owner: ModuleVolumeOwner) -> tuple[str, str, str]:
    return owner.system_id, owner.run_id, owner.operation_nonce


def reap_orphaned_module_volumes(
    conn: ModuleVolumeReaperConn,
    pool_name: str,
    identity_port: RemoteDeviceIdentityPort,
    *,
    retained_owners: Callable[[], Collection[ModuleVolumeOwner]],
) -> int:
    """Delete unretained owned volumes only after a complete host attachment preflight."""
    pool = _open_pool(conn, pool_name)
    owned = _enumerate_owned(pool, pool_name)
    retained_attempts = {_attempt(owner) for owner in retained_owners()}
    candidates = [item for item in owned if _attempt(item[1]) not in retained_attempts]
    if not candidates:
        return 0

    candidate_paths: list[tuple[str, _Volume, ModuleVolumeOwner]] = []
    for volume, owner in candidates:
        name = volume.name()
        try:
            candidate_paths.append(
                (
                    _normalize(volume.path(), pool=pool_name, volume=name),
                    volume,
                    owner,
                )
            )
        except libvirt.libvirtError as exc:
            raise _infra(
                "could not resolve remote module volume", pool=pool_name, volume=name
            ) from exc
    reference_paths = _reference_paths(conn)
    all_paths = {path for path, _, _ in candidate_paths} | reference_paths
    if len(all_paths) > _MAX_PATH_IDENTITIES:
        raise _infra("remote device identity lookup budget exceeded", pool=pool_name)

    identities: dict[str, RemoteDeviceIdentity] = {}
    candidate_by_path = {path: volume for path, volume, _ in candidate_paths}
    for path in sorted(all_paths):
        candidate = candidate_by_path.get(path)
        identities[path] = _identity(
            identity_port,
            path,
            pool=pool_name,
            volume=candidate.name() if candidate is not None else None,
        )
    referenced_identities = {identities[path] for path in reference_paths}

    removed = 0
    for path, volume, _owner in candidate_paths:
        name = volume.name()
        if identities[path] in referenced_identities:
            _LOG.warning(
                "remote module volume remains attached; skipping deletion",
                extra={"pool": pool_name, "volume": name},
            )
            continue
        try:
            volume.delete(0)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_NO_STORAGE_VOL:
                raise _infra(
                    "could not delete orphaned remote module volume",
                    pool=pool_name,
                    volume=name,
                ) from exc
        removed += 1
    return removed


__all__ = [
    "ModuleVolumeReaperConn",
    "list_owned_module_volumes",
    "reap_orphaned_module_volumes",
    "referenced_volume_paths",
]
