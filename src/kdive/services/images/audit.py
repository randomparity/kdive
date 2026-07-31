"""Shared audit composition for project-private image registration."""

from __future__ import annotations

from psycopg import AsyncConnection

from kdive.domain.catalog.images import ImageCatalogEntry
from kdive.security import audit

_UPLOAD_TOOL = "images.upload"
_OBJECT_KIND = "image_catalog"


async def record_private_registration(
    conn: AsyncConnection, entry: ImageCatalogEntry, principal: str
) -> None:
    """Audit a private image registration inside the caller's transaction."""
    if entry.owner is None:
        raise RuntimeError("registered private image has no owner project to audit under")
    await audit.record_system(
        conn,
        principal=principal,
        event=audit.AuditEvent(
            tool=_UPLOAD_TOOL,
            object_kind=_OBJECT_KIND,
            object_id=entry.id,
            transition="private-upload:registered",
            args={"provider": entry.provider, "name": entry.name, "arch": entry.arch},
            project=entry.owner,
        ),
    )
