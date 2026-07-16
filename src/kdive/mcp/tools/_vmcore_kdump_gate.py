"""Computed kdump-capability admission gate for ``vmcore.fetch`` (ADR-0361, #958).

Wires the ADR-0286/#957 computed kdump signal
(:func:`kdive.images.cataloging.capability_signals.render_kdump_signal`) into the vmcore
admission path: a kdump/fadump capture on a booted rootfs image whose computed kdump capability
is confidently negative is refused before a worker job is enqueued, instead of failing opaquely
deep in the capture. Every resolution uncertainty passes (fail open, ADR-0361) — the gate can
only *refuse* on a confident negative.
"""

from __future__ import annotations

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.components.references import CatalogComponentRef
from kdive.domain.catalog.images import ImageCatalogEntry, ImageState, ImageVisibility
from kdive.domain.errors import CategorizedError
from kdive.domain.lifecycle.records import System
from kdive.images.cataloging.capability_signals import render_kdump_signal
from kdive.images.kdump_support import DEFAULT_KERNEL_BASIS
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.serialization import JsonValue

# The computed kdump statuses that are a confident negative and therefore refuse: ``incapable``
# (the image's makedumpfile is provably too old for the kernel basis) and ``not_applicable`` (the
# image carries no kdump tooling — kernel-independent). Every other status (``capable``,
# ``unverified``) passes, so an absent/unparsable operand never blocks (ADR-0286 invariant).
_REFUSING_STATUSES: frozenset[str] = frozenset({"incapable", "not_applicable"})

# Mirrors ``catalog.resolve_public_rootfs_sync``: the local-libvirt catalog rootfs lane boots the
# one registered, public, arch-matched image (ADR-0228), so the gate reads the same image that
# booted rather than a private shadow the provision would never have selected.
_RESOLVE_PUBLIC_ARCH_SQL = """
    SELECT *
    FROM image_catalog
    WHERE provider = %(provider)s
      AND name = %(name)s
      AND arch = %(arch)s
      AND state = %(registered)s
      AND visibility = %(public)s
    LIMIT 1
"""


async def refusing_kdump_capability(
    conn: AsyncConnection, system: System
) -> dict[str, JsonValue] | None:
    """The computed kdump block iff the booted image is confidently incapable, else ``None``.

    Resolves the System's local-libvirt catalog rootfs to its registered ``image_catalog`` row and
    computes :func:`render_kdump_signal` against ``DEFAULT_KERNEL_BASIS`` (the characterized basis
    ``images.describe`` also defaults to — no booted-kernel version is persisted at admission,
    ADR-0361). Returns the rendered block (the refusal payload) only when its ``capability`` status
    is a confident negative; returns ``None`` — the caller then admits — on every resolution gap
    (unparsable profile, non-local-libvirt provider, non-catalog rootfs, no visible registered row)
    and on a ``capable``/``unverified`` status.
    """
    entry = await _resolve_catalog_rootfs(conn, system)
    if entry is None:
        return None
    block = render_kdump_signal(entry, DEFAULT_KERNEL_BASIS)
    status = block.get("capability")
    if isinstance(status, str) and status in _REFUSING_STATUSES:
        return block
    return None


async def _resolve_catalog_rootfs(
    conn: AsyncConnection, system: System
) -> ImageCatalogEntry | None:
    """Resolve the registered catalog image the System's local-libvirt rootfs names, or ``None``.

    Returns ``None`` (the gate then passes) for any resolution gap: an unparsable/unreadable
    profile, a non-local-libvirt provider section, a rootfs source that is not ``catalog``
    (``local``/``artifact``/``upload``), or no visible registered public row of the profile's arch.
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
