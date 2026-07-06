"""Operator configuration for the local-libvirt provider (ADR-0313, #1031).

The only per-Resource operator knob local-libvirt carries is ``guest_egress`` on the
``[[local_libvirt]]`` ``systems.toml`` block. It is resolved **per op** from that inventory by the
allocated Resource's ``name``, mirroring remote-libvirt's ``remote_config_for_resource`` (ADR-0187)
— so the runtime stays buildable without ``systems.toml`` and the knob is never read from the
allocation/provision request.

Unlike remote-libvirt — whose host connection config is *mandatory*, so a missing/malformed
inventory correctly fails the op closed — local-libvirt needs **no** ``systems.toml`` at all: every
op works without it and ``guest_egress`` defaults to the secure ``False`` (``restrict=on``). Because
the resolving seam (``rebind_for_resource``) runs for *every* local op (provision, debug-attach,
retrieve, introspect), a malformed file must not break an unrelated live op over an operator's typo.
This module therefore **degrades** a malformed inventory to the secure default with a logged
warning, matching the fault-isolation ``is_remote_libvirt_configured`` applies at the composition
gate (ADR-0112). The reconciler remains the loud, authoritative validator of the file.
"""

from __future__ import annotations

import logging

from kdive.inventory.errors import InventoryError
from kdive.inventory.loader import load_inventory_optional
from kdive.inventory.path import systems_toml_path

_logger = logging.getLogger(__name__)


def local_guest_egress_for_resource(resource_name: str) -> bool:
    """Resolve the operator ``guest_egress`` opt-in for the local Resource named ``resource_name``.

    The reconcile keys config-owned resources on ``(kind, name)`` (ADR-0112), so a local Resource's
    ``name`` is its ``[[local_libvirt]]`` instance name. Returns that instance's ``guest_egress``,
    or ``False`` (the secure default — ``restrict=on``) when the file is absent, declares no
    matching ``[[local_libvirt]]`` block, or is malformed. A malformed file is logged at WARNING and
    degraded, never raised: this seam runs for every local op and local needs no ``systems.toml``,
    so a corrupt inventory must not break an unrelated live op.

    Args:
        resource_name: The allocated local Resource's name (its ``[[local_libvirt]]`` block name).

    Returns:
        ``True`` only when a matching ``[[local_libvirt]]`` block sets ``guest_egress = true``.
    """
    try:
        doc = load_inventory_optional(systems_toml_path())
    except InventoryError as exc:
        _logger.warning(
            "systems.toml is present but invalid (%s); defaulting guest_egress off (restrict=on) "
            "for local resource %r — fix the inventory (the reconciler validates it)",
            exc,
            resource_name,
        )
        return False
    if doc is None:
        return False
    instance = next((inst for inst in doc.local_libvirt if inst.name == resource_name), None)
    if instance is None:
        _logger.debug(
            "no [[local_libvirt]] block named %r in systems.toml; guest_egress defaults off",
            resource_name,
        )
        return False
    return instance.guest_egress
