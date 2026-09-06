"""Attachment-safe remote module volume ownership and reaping (ADR-0588, ADR-0603)."""

from __future__ import annotations

import asyncio
import logging
import posixpath
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Collection, Sequence
from typing import Protocol

import libvirt
from defusedxml.common import DefusedXmlException

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority.device_identity import (
    build_remote_device_identity_port,
)
from kdive.providers.infra.reaping import ModuleVolumeKey
from kdive.providers.ports.authority import AuthorityRequestSender
from kdive.providers.remote_libvirt.config import RemoteAuthorityBinding, RemoteLibvirtConfig
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    RemoteDeviceIdentity,
    RemoteDeviceIdentityPort,
    path_references,
    volume_references,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volume_names import (
    ModuleVolumeOwner,
    parse_module_volume_name,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.xml_bounds import XmlEnumerationBudget
from kdive.providers.remote_libvirt.reaping.connections import (
    FleetConnections,
    map_over_fleet,
    open_libvirt_reaper,
    remote_libvirt_reaper_connections,
)
from kdive.security.secrets.secret_registry import SecretRegistry

_MAX_STORAGE_PATH_BYTES = 4096
_MAX_IDENTITY_COMPONENT = (1 << 64) - 1
_MAX_PATH_IDENTITIES = 4096
_HOST_SWEEP_SECONDS = 300.0
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


def _admit_path(paths: set[str], path: str, *, pool: str) -> None:
    paths.add(path)
    if len(paths) > _MAX_PATH_IDENTITIES:
        raise _infra("remote device identity lookup budget exceeded", pool=pool)


def _reference_paths(
    conn: ModuleVolumeReaperConn, *, admitted_paths: set[str] | None = None
) -> set[str]:
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
                normalized = _normalize(path)
                result.add(normalized)
                if admitted_paths is not None:
                    _admit_path(admitted_paths, normalized, pool="remote-libvirt")
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
                normalized = _normalize(path, pool=pool_name, volume=volume_name)
                result.add(normalized)
                if admitted_paths is not None:
                    _admit_path(admitted_paths, normalized, pool=pool_name)
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


def _deadline_not_expired(deadline: float | None, clock: Callable[[], float], *, pool: str) -> None:
    if deadline is not None and clock() >= deadline:
        raise _infra("remote module volume reaping deadline expired", pool=pool)


def reap_orphaned_module_volumes(
    conn: ModuleVolumeReaperConn,
    pool_name: str,
    identity_port: RemoteDeviceIdentityPort,
    *,
    retained_owners: Callable[[], Collection[ModuleVolumeOwner]],
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Delete unretained owned volumes only after a complete host attachment preflight."""
    pool = _open_pool(conn, pool_name)
    owned = _enumerate_owned(pool, pool_name)
    retained = set(retained_owners())
    candidates = [item for item in owned if item[1] not in retained]
    if not candidates:
        return 0

    candidate_paths: list[tuple[str, _Volume, ModuleVolumeOwner]] = []
    admitted_paths: set[str] = set()
    for volume, owner in candidates:
        name = volume.name()
        try:
            path = _normalize(volume.path(), pool=pool_name, volume=name)
            _admit_path(admitted_paths, path, pool=pool_name)
            candidate_paths.append((path, volume, owner))
        except libvirt.libvirtError as exc:
            raise _infra(
                "could not resolve remote module volume", pool=pool_name, volume=name
            ) from exc
    _deadline_not_expired(deadline, clock, pool=pool_name)
    reference_paths = _reference_paths(conn, admitted_paths=admitted_paths)
    all_paths = admitted_paths

    identities: dict[str, RemoteDeviceIdentity] = {}
    candidate_by_path = {path: volume for path, volume, _ in candidate_paths}
    for path in sorted(all_paths):
        _deadline_not_expired(deadline, clock, pool=pool_name)
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
        _deadline_not_expired(deadline, clock, pool=pool_name)
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


class RemoteLibvirtModuleVolumeReaper:
    """Completion-owned fleet adapter for synchronous host module-volume sweeps."""

    def __init__(
        self,
        connections: FleetConnections[ModuleVolumeReaperConn],
        executor: RemoteModulePreparationExecutor,
        authority_sender_factory: Callable[[RemoteAuthorityBinding], AuthorityRequestSender] | None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connections = connections
        self._executor = executor
        self._authority_sender_factory = authority_sender_factory
        self._clock = clock

    @classmethod
    def from_env(
        cls,
        *,
        secret_registry: SecretRegistry,
        authority_sender_factory: Callable[[RemoteAuthorityBinding], AuthorityRequestSender] | None,
    ) -> RemoteLibvirtModuleVolumeReaper:
        """Construct without opening a host connection or resolving authority credentials."""
        return cls(
            remote_libvirt_reaper_connections(
                secret_registry=secret_registry,
                open_connection=open_libvirt_reaper,
            ),
            RemoteModulePreparationExecutor(),
            authority_sender_factory,
        )

    async def reap_module_volumes(
        self,
        retained_owners: Callable[[], Awaitable[Collection[ModuleVolumeKey]]],
    ) -> int:
        loop = asyncio.get_running_loop()

        def retained_sync() -> Collection[ModuleVolumeOwner]:
            async def read_retained() -> Collection[ModuleVolumeKey]:
                return await retained_owners()

            keys = asyncio.run_coroutine_threadsafe(read_retained(), loop).result()
            return [ModuleVolumeOwner(*key) for key in keys]

        def sweep() -> int:
            def reap_host(conn: ModuleVolumeReaperConn, config: RemoteLibvirtConfig) -> int:
                if config.authority is None or self._authority_sender_factory is None:
                    raise _conflict("remote module provider authority is unavailable")
                sender = self._authority_sender_factory(config.authority)
                deadline = self._clock() + _HOST_SWEEP_SECONDS
                identity = build_remote_device_identity_port(sender, deadline)
                if identity is None:
                    raise _conflict("remote module provider authority is unavailable")
                return reap_orphaned_module_volumes(
                    conn,
                    config.storage_pool,
                    identity,
                    retained_owners=retained_sync,
                    deadline=deadline,
                    clock=self._clock,
                )

            results = map_over_fleet(
                self._connections,
                reap_host,
                operation="module-volume reaping",
            )
            if self._connections.configs() and not results:
                raise _infra("no remote module-volume host was reachable")
            return sum(results)

        return await self._executor.run(sweep)


__all__ = [
    "ModuleVolumeReaperConn",
    "RemoteLibvirtModuleVolumeReaper",
    "list_owned_module_volumes",
    "reap_orphaned_module_volumes",
    "referenced_volume_paths",
]
