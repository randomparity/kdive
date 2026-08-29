"""Remote external-boot artifact ownership and orphan cleanup (ADR-0583, #2119)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Collection
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.boot_artifact_volumes import (
    BOOT_ARTIFACT_METADATA_NS,
    BootArtifactKind,
    artifact_volume_name,
)

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PARTIAL = re.compile(
    r"^(kdive-(?:kernel|initrd)-[0-9a-f-]{36}-[0-9a-f-]{36})-partial-"
    r"([0-9a-f-]{36})-([0-9a-f]{64})$"
)


@dataclass(frozen=True, slots=True)
class BootArtifactVolume:
    """A metadata-verified KDIVE artifact volume found in a configured pool."""

    name: str
    kind: BootArtifactKind
    system_id: UUID
    run_id: UUID
    digest: str
    partial: bool
    attempt_id: UUID | None

    @property
    def owner(self) -> tuple[BootArtifactKind, UUID, UUID, str]:
        return self.kind, self.system_id, self.run_id, self.digest


class _Volume(Protocol):
    def name(self) -> str: ...
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802
    def delete(self, flags: int = 0) -> int: ...


class _Pool(Protocol):
    def refresh(self, flags: int = 0) -> int: ...
    def listAllVolumes(self, flags: int = 0) -> list[_Volume]: ...  # noqa: N802


class BootArtifactReaperConn(Protocol):
    def storagePoolLookupByName(self, name: str) -> _Pool: ...  # noqa: N802


def _metadata(xml: str) -> dict[str, str] | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    marker = root.find(f"./metadata/{{{BOOT_ARTIFACT_METADATA_NS}}}artifact")
    if marker is None or marker.tag != f"{{{BOOT_ARTIFACT_METADATA_NS}}}artifact":
        return None
    values = dict(marker.attrib)
    required = {"kind", "system-id", "run-id", "sha256"}
    if not set(values) <= required | {"attempt-id"} or not required <= values.keys():
        return None
    return values


def _parse(volume: _Volume) -> BootArtifactVolume | None:
    name = volume.name()
    values = _metadata(volume.XMLDesc(0))
    if values is None or values["kind"] not in {"kernel", "initrd"}:
        return None
    if not _DIGEST.fullmatch(values["sha256"]):
        return None
    try:
        system_id = UUID(values["system-id"])
        run_id = UUID(values["run-id"])
    except ValueError:
        return None
    kind = values["kind"]
    artifact_kind = kind
    expected = artifact_volume_name(artifact_kind, system_id, run_id)
    partial = False
    attempt_id: UUID | None = None
    if name != expected:
        match = _PARTIAL.fullmatch(name)
        if match is None or match.group(1) != expected:
            return None
        try:
            attempt_id = UUID(match.group(2))
        except ValueError:
            return None
        if match.group(3) != values["sha256"][len("sha256:") :]:
            return None
        if values.get("attempt-id") != str(attempt_id):
            return None
        partial = True
    elif "attempt-id" in values:
        return None
    return BootArtifactVolume(
        name=name,
        kind=artifact_kind,
        system_id=system_id,
        run_id=run_id,
        digest=values["sha256"],
        partial=partial,
        attempt_id=attempt_id,
    )


def list_owned_boot_artifacts(
    conn: BootArtifactReaperConn, pool_name: str
) -> list[BootArtifactVolume]:
    """List only volumes with a valid KDIVE name and matching ownership metadata."""
    try:
        pool = conn.storagePoolLookupByName(pool_name)
        pool.refresh(0)
        return [artifact for volume in pool.listAllVolumes(0) if (artifact := _parse(volume))]
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
    """Delete metadata-verified artifacts not present in the durable live-owner set.

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
    removed = 0
    live = set(live_owners)
    for volume in pool.listAllVolumes(0):
        artifact = _parse(volume)
        if artifact is None or artifact.owner in live:
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


__all__ = [
    "BootArtifactReaperConn",
    "BootArtifactVolume",
    "list_owned_boot_artifacts",
    "reap_orphaned_boot_artifacts",
]
