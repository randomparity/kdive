"""Async image-catalog resolver (ADR-0092, ADR-0093).

Replaces the synchronous YAML rootfs lookup. Resolution returns one ``registered`` image
visible to the calling project: a same-name project-private image shadows the public one
(``private`` first), so the result is deterministic. ``defined`` and ``pending`` rows are never
returned — only a fully published (``registered``) image is bootable.
"""

from __future__ import annotations

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.components.references import CatalogComponentRef
from kdive.domain.catalog.images import ImageCatalogEntry, ImageState, ImageVisibility
from kdive.domain.errors import CategorizedError
from kdive.domain.lifecycle.records import System
from kdive.images.cataloging.projection import IMAGE_CATALOG_ENTRY_PROJECTION
from kdive.images.planes.base import PROVENANCE_OS_RELEASE
from kdive.profiles.provisioning import ProvisioningProfile

# Order by visibility so the project's private row (if any) sorts before the public one; the
# resolver takes the first. `private` < `public` lexically, so the explicit CASE keeps the
# intent legible and independent of the enum spelling.
_RESOLVE_SQL = f"""
    SELECT {IMAGE_CATALOG_ENTRY_PROJECTION}
    FROM image_catalog
    WHERE provider = %(provider)s
      AND name = %(name)s
      AND state = %(registered)s
      AND (visibility = %(public)s OR (visibility = %(private)s AND owner = %(project)s))
    ORDER BY CASE WHEN visibility = %(private)s THEN 0 ELSE 1 END
    LIMIT 1
"""


async def resolve_rootfs(
    conn: AsyncConnection, provider: str, name: str, *, project: str
) -> ImageCatalogEntry | None:
    """Resolve one registered rootfs image visible to ``project``.

    Returns the project's private image first (private shadows public on the same
    ``(provider, name)``); otherwise the public image; else ``None``. Only ``registered``
    rows are returned, so a ``defined``- or ``pending``-only baseline resolves to ``None``.

    Args:
        conn: An async Postgres connection.
        provider: The provider key (e.g. ``local-libvirt``).
        name: The catalog image name.
        project: The owning project the caller resolves on behalf of.

    Returns:
        The resolved :class:`ImageCatalogEntry`, or ``None`` if no registered image is visible.
    """
    params = {
        "provider": provider,
        "name": name,
        "registered": ImageState.REGISTERED.value,
        "public": ImageVisibility.PUBLIC.value,
        "private": ImageVisibility.PRIVATE.value,
        "project": project,
    }
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RESOLVE_SQL, params)
        row = await cur.fetchone()
    return None if row is None else ImageCatalogEntry.model_validate(row)


_RESOLVE_PUBLIC_ARCH_SQL = f"""
    SELECT {IMAGE_CATALOG_ENTRY_PROJECTION}
    FROM image_catalog
    WHERE provider = %(provider)s
      AND name = %(name)s
      AND arch = %(arch)s
      AND state = %(registered)s
      AND visibility = %(public)s
    LIMIT 1
"""


def resolve_public_rootfs_sync(
    conn: psycopg.Connection, provider: str, name: str, arch: str
) -> ImageCatalogEntry | None:
    """Resolve the one registered, public, arch-matched rootfs image (sync, public-scope).

    The local-libvirt catalog rootfs lane resolves public images only (ADR-0228); ``arch`` makes
    the match deterministic via the ``(provider, name, arch)`` unique index. A private image is
    never returned, and a name with no registered public row of that ``arch`` resolves to ``None``.

    Args:
        conn: A synchronous Postgres connection (the provision seam owns no async pool).
        provider: The provider key (e.g. ``local-libvirt``).
        name: The catalog image name.
        arch: The provisioning profile's target arch, matched exactly.

    Returns:
        The resolved :class:`ImageCatalogEntry`, or ``None`` if none is visible.
    """
    params = {
        "provider": provider,
        "name": name,
        "arch": arch,
        "registered": ImageState.REGISTERED.value,
        "public": ImageVisibility.PUBLIC.value,
    }
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_RESOLVE_PUBLIC_ARCH_SQL, params)
        row = cur.fetchone()
    return None if row is None else ImageCatalogEntry.model_validate(row)


async def resolve_system_catalog_rootfs(
    conn: AsyncConnection, system: System
) -> ImageCatalogEntry | None:
    """The registered public arch-matched image a System's local-libvirt catalog rootfs names.

    The same row the local-libvirt catalog lane boots (ADR-0228), so a reader sees the image that
    booted rather than a private shadow the provision would never have selected. Returns ``None``
    on every resolution gap: an unparsable profile, a non-local-libvirt section, a rootfs that is
    not ``catalog`` (``local``/``artifact``/``upload``), or no visible registered row of the
    profile's arch (ADR-0361, ADR-0678).
    """
    try:
        profile = ProvisioningProfile.parse(system.provisioning_profile)
    except CategorizedError:
        return None
    section = profile.provider.local_libvirt_section
    if section is None or not isinstance(section.rootfs, CatalogComponentRef):
        return None
    params = {
        "provider": section.rootfs.provider,
        "name": section.rootfs.name,
        "arch": profile.arch,
        "registered": ImageState.REGISTERED.value,
        "public": ImageVisibility.PUBLIC.value,
    }
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_RESOLVE_PUBLIC_ARCH_SQL, params)
        row = await cur.fetchone()
    return None if row is None else ImageCatalogEntry.model_validate(row)


def image_os_id(entry: ImageCatalogEntry) -> str | None:
    """The build-recorded os-release ``ID`` (ADR-0311), or ``None`` when absent or malformed."""
    record = entry.provenance.get(PROVENANCE_OS_RELEASE)
    os_id = record.get("id") if isinstance(record, dict) else None
    return os_id if isinstance(os_id, str) and os_id else None
