"""Remote-libvirt resource-detail projection for ``resources.describe``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.catalog.images import ImageVisibility
from kdive.domain.catalog.resources import ResourceKind
from kdive.serialization import JsonValue

type StagedVolumeProbe = Callable[[list[str]], Awaitable[dict[str, str]]]

_STAGED_IMAGES_SQL = """
    SELECT name, volume
    FROM image_catalog
    WHERE provider = %(provider)s
      AND volume IS NOT NULL
      AND (visibility = %(public)s
           OR (visibility = %(private)s AND owner = ANY(%(projects)s)))
    ORDER BY name, arch
"""


async def project_resource_details(
    pool: AsyncConnectionPool,
    viewer_projects: tuple[str, ...],
    *,
    staged_probe: StagedVolumeProbe,
) -> dict[str, JsonValue]:
    images = await _staged_remote_images(pool, viewer_projects)
    if not images:
        return {"staged_base_images": []}
    statuses = await staged_probe([volume for _, volume in images])
    staged_base_images: list[JsonValue] = [
        {"name": name, "volume": volume, "staged": statuses.get(volume, "unknown")}
        for name, volume in images
    ]
    return {"staged_base_images": staged_base_images}


async def _staged_remote_images(
    pool: AsyncConnectionPool, viewer_projects: tuple[str, ...]
) -> list[tuple[str, str]]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _STAGED_IMAGES_SQL,
            {
                "provider": ResourceKind.REMOTE_LIBVIRT.value,
                "public": ImageVisibility.PUBLIC.value,
                "private": ImageVisibility.PRIVATE.value,
                "projects": list(viewer_projects),
            },
        )
        return [(row["name"], row["volume"]) for row in await cur.fetchall()]
