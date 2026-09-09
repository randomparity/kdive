"""Accelerator-keyed deadline scaling for the local-libvirt provider (ADR-0341).

TCG (software-emulated, foreign-arch) guests execute an order of magnitude slower than
KVM-accelerated ones, so boot-readiness deadlines tuned for KVM time out spuriously under
TCG. This module holds the single multiplier the provider applies where a guest-execution
deadline is computed, keyed off the System's persisted ``accel`` fact (#1141), so the policy
lives in one place rather than as scattered per-step constants.
"""

from __future__ import annotations

from collections.abc import Callable

import kdive.config as config
from kdive.diagnostics.contributions.guest_arch_accel import (
    kvm_probe_for_uri,
    resolved_libvirt_uri,
)
from kdive.providers.local_libvirt.settings import LIBVIRT_TCG_DEADLINE_MULTIPLIER


def tcg_deadline_multiplier(accel: str | None) -> float:
    """Return 1 for KVM and the safe TCG multiplier for all other accelerators (ADR-0341)."""
    if accel == "kvm":
        return 1.0
    return config.require(LIBVIRT_TCG_DEADLINE_MULTIPLIER)


def host_appliance_multiplier(*, kvm_present: Callable[[], bool] | None = None) -> float:
    """Return the same multiplier keyed off the WORKER HOST's KVM rather than a System (#2383).

    :func:`tcg_deadline_multiplier` keys off the System's persisted ``accel`` because the guest
    boots under that accelerator. A libguestfs appliance is not the System: it is a *host-arch*
    VM the worker boots on its own host to edit a disk, so what sets its speed is whether the
    worker host has usable KVM — a ppc64le System under TCG on an x86_64 KVM host still gets a
    fast appliance, while any System on a host without ``/dev/kvm`` gets an emulated one. Both
    budgets scale by the same factor from different keys.

    Measured on an emulated-POWER host (#2383): ``virt-customize --ssh-inject`` against a
    Fedora 44 ppc64le overlay took **1474 s** and exited 0 — appliance boot to key inject
    734 s, SELinux relabel a further 573 s — against a 300 s budget it could never meet.

    ``kvm_present`` is injected so both branches are unit-tested without a real ``/dev/kvm``.
    """
    probe = kvm_present if kvm_present is not None else kvm_probe_for_uri(resolved_libvirt_uri())
    return tcg_deadline_multiplier("kvm" if probe() else None)
