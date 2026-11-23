"""Shared bounded libvirt attachment proofs and host identity (ADRs 0585, 0603)."""

from __future__ import annotations

import os
import posixpath
import stat
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from typing import Protocol
from uuid import UUID

import libvirt
from defusedxml.common import DefusedXmlException

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.module_operation import RemoteDeviceIdentity, RemoteDeviceIdentityPort
from kdive.providers.shared.libvirt_xml import (
    KDIVE_METADATA_NS,
    remote_metadata_system_id,
)
from kdive.providers.shared.libvirt_xml_bounds import (
    MAX_LIBVIRT_XML_DOCUMENTS,
    XmlEnumerationBudget,
)
from kdive.providers.shared.runtime_paths import domain_name_for

_MAX_STORAGE_PATH_BYTES = 4096
_MAX_IDENTITY_COMPONENT = (1 << 64) - 1
MAX_PATH_IDENTITIES = 4096


class Domain(Protocol):
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802
    def isActive(self) -> int: ...  # noqa: N802
    def isPersistent(self) -> int: ...  # noqa: N802


class StorageVolume(Protocol):
    def path(self) -> str: ...


class StoragePool(Protocol):
    def storageVolLookupByName(self, name: str) -> StorageVolume: ...  # noqa: N802


class AttachmentConn(Protocol):
    def listAllDomains(self, flags: int = 0) -> Sequence[Domain]: ...  # noqa: N802
    def storagePoolLookupByName(self, name: str) -> StoragePool: ...  # noqa: N802


class HostStatDeviceIdentity:
    """Server-side ADR-0603 adapter backed by a following ``stat(2)`` lookup."""

    def __init__(self, stat_path: Callable[..., os.stat_result] = os.stat) -> None:
        self._stat_path = stat_path

    def identity(self, path: str) -> RemoteDeviceIdentity | None:
        normalized = normalize_storage_path(path)
        try:
            result = self._stat_path(normalized, follow_symlinks=True)
        except FileNotFoundError:
            return None
        except OSError:
            raise CategorizedError(
                "remote device identity lookup failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from None
        if stat.S_ISBLK(result.st_mode):
            return RemoteDeviceIdentity(kind="block", primary=result.st_rdev, secondary=0)
        return RemoteDeviceIdentity(kind="inode", primary=result.st_dev, secondary=result.st_ino)


def attachment_conflict(message: str, **details: object) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFLICT, details=details)


def attachment_infrastructure(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.INFRASTRUCTURE_FAILURE)


def volume_references(root: ET.Element) -> list[tuple[str, str]]:
    """Return pool/volume pairs from every storage source in each disk graph."""
    references = []
    for disk in root.findall("./devices/disk"):
        for source in disk.iter("source"):
            pool = source.get("pool")
            volume = source.get("volume")
            if pool is not None and volume is not None:
                references.append((pool, volume))
    return references


def path_references(root: ET.Element) -> set[str]:
    """Return host paths from every source and legacy mirror in each disk graph."""
    return set(path_reference_list(root))


def path_reference_list(root: ET.Element) -> list[str]:
    """Every path in each disk graph, repeats kept so a duplicate reference stays countable."""
    paths = []
    for disk in root.findall("./devices/disk"):
        for source in disk.iter("source"):
            path = source.get("file") or source.get("dev")
            if path is not None:
                paths.append(path)
        for mirror in disk.iter("mirror"):
            path = mirror.get("file") or mirror.get("dev")
            if path is not None:
                paths.append(path)
    return paths


def system_ownership(root: ET.Element, *, domain: str | None) -> str | None:
    tags = [
        *root.findall(f"./metadata/{{{KDIVE_METADATA_NS}}}system"),
        *root.findall(f"./metadata/{{{KDIVE_METADATA_NS}}}domain/{{{KDIVE_METADATA_NS}}}system"),
    ]
    if len(tags) > 1:
        raise attachment_conflict("duplicate System ownership metadata", domain=domain)
    if not tags:
        return None
    grouped = remote_metadata_system_id(root)
    direct = (tags[0].text or "").strip()
    return grouped or direct or None


def prove_no_foreign_storage_references(
    conn: AttachmentConn,
    identity_port: RemoteDeviceIdentityPort,
    owner_system_id: str,
    protected: frozenset[tuple[str, str]],
) -> None:
    """Reject any foreign domain graph that aliases a volume about to be destroyed."""
    _prove_no_foreign_references(
        conn,
        identity_port,
        owner_system_id,
        protected_volumes=protected,
        protected_paths=frozenset(),
    )


def prove_no_foreign_path_references(
    conn: AttachmentConn,
    identity_port: RemoteDeviceIdentityPort,
    owner_system_id: str,
    protected_paths: frozenset[str],
) -> None:
    """Reject foreign disk-graph aliases of exact host paths about to be removed.

    This is the filesystem-path counterpart of :func:`prove_no_foreign_storage_references`.
    Both use the same bounded traversal of every live and inactive definition, recursive
    ``source``/``mirror`` graph references, and following device identity comparison.
    """
    _prove_no_foreign_references(
        conn,
        identity_port,
        owner_system_id,
        protected_volumes=frozenset(),
        protected_paths=protected_paths,
    )


def _prove_no_foreign_references(
    conn: AttachmentConn,
    identity_port: RemoteDeviceIdentityPort,
    owner_system_id: str,
    *,
    protected_volumes: frozenset[tuple[str, str]],
    protected_paths: frozenset[str],
) -> None:
    """Traverse all defined disk graphs once against protected volumes and host paths."""
    identities: dict[str, RemoteDeviceIdentity] = {}

    def resolve(path: str) -> RemoteDeviceIdentity:
        normalized = normalize_storage_path(path)
        if normalized not in identities:
            if len(identities) >= MAX_PATH_IDENTITIES:
                raise attachment_infrastructure("remote device identity lookup budget exceeded")
            identities[normalized] = device_identity(identity_port, normalized)
        return identities[normalized]

    protected_identities = {
        resolve(path) for path in sorted(normalize_storage_path(path) for path in protected_paths)
    }
    for pool, volume in sorted(protected_volumes):
        try:
            path = volume_path(conn, pool, volume)
        except CategorizedError as error:
            cause = error.__cause__
            if isinstance(cause, libvirt.libvirtError) and (
                cause.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL
            ):
                continue
            raise
        protected_identities.add(resolve(path))
    try:
        domains = conn.listAllDomains(0)
    except libvirt.libvirtError as exc:
        raise attachment_infrastructure("could not enumerate remote teardown attachments") from exc
    if len(domains) > MAX_LIBVIRT_XML_DOCUMENTS:
        error = attachment_infrastructure(
            "remote teardown domain enumeration exceeds provider limit"
        )
        for cleanup_error in _release_domains(domains, set()):
            error.add_note(f"cleanup failed: {cleanup_error!r}")
        raise error
    budget = XmlEnumerationBudget()
    seen_names: set[str] = set()
    released: set[int] = set()
    cleanup_errors: list[Exception] = []
    try:
        for domain in domains:
            try:
                try:
                    active = bool(domain.isActive())
                    documents = [domain.XMLDesc(0)]
                    if active and domain.isPersistent():
                        documents.append(domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
                except libvirt.libvirtError as exc:
                    raise attachment_infrastructure(
                        "could not read a remote teardown attachment"
                    ) from exc
                for index, document in enumerate(documents):
                    try:
                        root = budget.parse(document)
                    except (ET.ParseError, DefusedXmlException, ValueError) as exc:
                        raise attachment_conflict(
                            "could not inspect a remote teardown attachment"
                        ) from exc
                    name = root.findtext("name")
                    if index == 0:
                        if not name or name in seen_names:
                            raise attachment_conflict("duplicate or unnamed remote teardown domain")
                        seen_names.add(name)
                    owner = system_ownership(root, domain=name)
                    references = volume_references(root)
                    paths = path_references(root)
                    paths.update(
                        value
                        for value in (root.findtext("./os/kernel"), root.findtext("./os/initrd"))
                        if value is not None
                    )
                    document_identities = {resolve(path) for path in paths}
                    document_identities.update(
                        resolve(volume_path(conn, pool, volume)) for pool, volume in references
                    )
                    direct = set(references) & set(protected_volumes)
                    aliases = document_identities & protected_identities
                    exact_owner = owner == owner_system_id and name == domain_name_for(
                        UUID(owner_system_id)
                    )
                    if (direct or aliases) and not exact_owner:
                        raise attachment_conflict(
                            "another domain references remote teardown storage", domain=name
                        )
            finally:
                cleanup_errors.extend(_release_domains((domain,), released))
    except BaseException as error:
        cleanup_errors.extend(_release_domains(domains, released))
        for cleanup_error in cleanup_errors:
            error.add_note(f"cleanup failed: {cleanup_error!r}")
        raise
    if cleanup_errors:
        raise ExceptionGroup("remote teardown domain cleanup failed", cleanup_errors)


def _release_domains(domains: Sequence[Domain], released: set[int]) -> list[Exception]:
    errors: list[Exception] = []
    for domain in domains:
        identifier = id(domain)
        if identifier in released:
            continue
        released.add(identifier)
        closer = getattr(domain, "free", None)
        if callable(closer):
            try:
                closer()
            except Exception as error:
                errors.append(error)
    return errors


def volume_path(conn: AttachmentConn, pool_name: str, volume_name: str) -> str:
    try:
        pool = conn.storagePoolLookupByName(pool_name)
        path = pool.storageVolLookupByName(volume_name).path()
    except libvirt.libvirtError as exc:
        raise attachment_infrastructure("could not resolve remote module volume path") from exc
    return normalize_storage_path(path, pool=pool_name, volume=volume_name)


def normalize_storage_path(path: str, **details: object) -> str:
    if not path.startswith("/") or "\x00" in path or len(path.encode()) > _MAX_STORAGE_PATH_BYTES:
        raise attachment_conflict("remote module storage path is invalid", **details)
    return posixpath.normpath("/" + path.lstrip("/"))


def device_identity(identity_port: RemoteDeviceIdentityPort, path: str) -> RemoteDeviceIdentity:
    normalized = normalize_storage_path(path)
    try:
        identity = identity_port.identity(normalized)
    except Exception:
        raise attachment_infrastructure("remote device identity lookup failed") from None
    if identity is None:
        raise attachment_conflict("remote device identity is unavailable")
    if (
        type(identity) is not RemoteDeviceIdentity
        or identity.kind not in {"inode", "block"}
        or type(identity.primary) is not int
        or type(identity.secondary) is not int
        or not 0 <= identity.primary <= _MAX_IDENTITY_COMPONENT
        or not 0 <= identity.secondary <= _MAX_IDENTITY_COMPONENT
        or (identity.kind == "block" and identity.secondary != 0)
    ):
        raise attachment_conflict("remote device identity is invalid")
    return identity
