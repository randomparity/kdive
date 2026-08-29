"""System-time binding of mechanically verified image root provenance (ADR-0583)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.ports.external_boot import RootSpecV1


@dataclass(frozen=True, slots=True)
class RootProvenanceSnapshot:
    source_image_id: UUID
    project: str
    architecture: str
    image_digest: str
    root_spec: RootSpecV1


async def resolve_root_provenance(
    conn: AsyncConnection, profile: ProvisioningProfile, project: str
) -> RootProvenanceSnapshot | None:
    """Resolve one visible, verified catalog authority for a checksum-pinned remote source.

    Missing checksum/catalog/root facts are legacy disk/GRUB-only behavior. A visible purported
    authority is rejected when ambiguous or internally inconsistent.
    """
    remote = profile.provider.remote_libvirt_section
    source = remote.base_image_source if remote is not None else None
    if source is None or source.sha256 is None:
        return None
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, arch, digest, provenance FROM image_catalog "
            "WHERE provider = 'remote-libvirt' AND state = 'registered' AND digest = %s "
            "AND (visibility = 'public' OR (visibility = 'private' AND owner = %s)) "
            "ORDER BY id FOR SHARE",
            (source.sha256, project),
        )
        rows = await cur.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise CategorizedError(
            "multiple visible catalog images match the remote rootfs digest; remove the duplicate "
            "catalog authority and retry provisioning",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "ambiguous_root_provenance"},
        )
    row = rows[0]
    provenance = row["provenance"]
    raw_root = provenance.get("root_spec") if isinstance(provenance, dict) else None
    if raw_root is None:
        return None
    try:
        root_spec = RootSpecV1.model_validate(raw_root)
    except ValidationError as exc:
        raise CategorizedError(
            "catalog root provenance is malformed; rebuild the image and retry provisioning",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "malformed_root_provenance"},
        ) from exc
    argument_keys = [argument.split("=", 1)[0] for argument in root_spec.arguments]
    if (
        argument_keys.count("root") != 1
        or len(argument_keys) != len(set(argument_keys))
        or root_spec.arguments.count(f"root={root_spec.root}") != 1
    ):
        raise CategorizedError(
            "catalog root provenance has conflicting storage arguments; rebuild the image and "
            "retry provisioning",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "conflicting_root_provenance_arguments"},
        )
    digest = str(row["digest"])
    architecture = str(row["arch"])
    if root_spec.authority != "stage-inspection" or root_spec.source.kind != "staged-image":
        raise CategorizedError(
            "catalog root provenance is not mechanically verified; use disk/GRUB boot or rebuild",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "unverified_root_provenance"},
        )
    if root_spec.source.identity != digest:
        raise CategorizedError(
            "catalog root provenance is stale; rebuild the image and retry provisioning",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "stale_root_provenance"},
        )
    if architecture != profile.arch or root_spec.architecture != profile.arch:
        raise CategorizedError(
            "catalog root provenance architecture does not match the System profile; rebuild for "
            "the requested architecture or use disk/GRUB boot",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "root_provenance_architecture_mismatch"},
        )
    return RootProvenanceSnapshot(
        source_image_id=UUID(str(row["id"])),
        project=project,
        architecture=architecture,
        image_digest=digest,
        root_spec=root_spec,
    )


async def insert_root_provenance(
    conn: AsyncConnection, system_id: UUID, snapshot: RootProvenanceSnapshot
) -> None:
    """Persist the immutable snapshot in the open System admission transaction."""
    await conn.execute(
        "INSERT INTO system_root_provenance "
        "(system_id, source_image_id, project, architecture, image_digest, root_spec) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            system_id,
            snapshot.source_image_id,
            snapshot.project,
            snapshot.architecture,
            snapshot.image_digest,
            Jsonb(snapshot.root_spec.model_dump(mode="json", by_alias=True)),
        ),
    )


__all__ = ["RootProvenanceSnapshot", "insert_root_provenance", "resolve_root_provenance"]
