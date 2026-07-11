"""Shared visible-image lookup for catalog read tools."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.catalog.images import ImageCatalogEntry, ImageVisibility
from kdive.images.planes.base import PROVENANCE_DEFAULT_KERNEL_VERSION
from kdive.log import bind_context
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, projects_with_role

_VISIBLE_IMAGE_SQL = """
    SELECT *
    FROM image_catalog
    WHERE id = %(id)s
      AND (visibility = %(public)s
           OR (visibility = %(private)s AND owner = ANY(%(projects)s)))
"""


async def fetch_visible_image(
    pool: AsyncConnectionPool, ctx: RequestContext, uid: UUID
) -> ImageCatalogEntry | None:
    """Return the catalog row visible to ``ctx``, or ``None`` for absent/invisible ids."""
    with bind_context(principal=ctx.principal):
        params = {
            "id": str(uid),
            "public": ImageVisibility.PUBLIC.value,
            "private": ImageVisibility.PRIVATE.value,
            "projects": projects_with_role(ctx, Role.VIEWER),
        }
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_VISIBLE_IMAGE_SQL, params)
            row = await cur.fetchone()
    return ImageCatalogEntry.model_validate(row) if row is not None else None


def default_kernel_version(provenance: dict[str, Any]) -> str:
    """Return the build-recorded default kernel version, or ``""`` when absent."""
    value = provenance.get(PROVENANCE_DEFAULT_KERNEL_VERSION)
    return str(value) if value else ""
