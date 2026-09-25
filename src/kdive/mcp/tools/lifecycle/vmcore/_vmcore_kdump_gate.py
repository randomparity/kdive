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

from kdive.domain.lifecycle.records import System
from kdive.images.cataloging.capability_signals import render_kdump_signal
from kdive.images.cataloging.catalog import resolve_system_catalog_rootfs
from kdive.images.kdump_support import DEFAULT_KERNEL_BASIS
from kdive.serialization import JsonValue

# The computed kdump statuses that are a confident negative and therefore refuse: ``incapable``
# (the image's makedumpfile is provably too old for the kernel basis) and ``not_applicable`` (the
# image carries no kdump tooling — kernel-independent). Every other status (``capable``,
# ``unverified``) passes, so an absent/unparsable operand never blocks (ADR-0286 invariant).
_REFUSING_STATUSES: frozenset[str] = frozenset({"incapable", "not_applicable"})


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
    entry = await resolve_system_catalog_rootfs(conn, system)
    if entry is None:
        return None
    block = render_kdump_signal(entry, DEFAULT_KERNEL_BASIS)
    status = block.get("capability")
    if isinstance(status, str) and status in _REFUSING_STATUSES:
        return block
    return None
