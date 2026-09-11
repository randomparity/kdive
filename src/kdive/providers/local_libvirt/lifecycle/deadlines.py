"""Accelerator-keyed deadline scaling for the local-libvirt provider (ADR-0341, ADR-0636).

Software-emulated execution runs an order of magnitude slower than KVM-accelerated execution,
so a budget tuned for KVM times out spuriously without it. This module holds the single
multiplier the provider applies, and the two keys that select it:

* :func:`tcg_deadline_multiplier` — the System's persisted ``accel`` fact (#1141), for a
  guest-execution deadline, because the guest executes under that accelerator.
* :func:`host_appliance_multiplier` — the worker host's KVM (#2383), for a host-side
  libguestfs appliance budget, because the appliance is a host-arch VM the worker boots.

Keeping both here keeps the policy in one place rather than as scattered per-step constants.
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

    Further points from the same host class (#2414), taken through the sibling budgets in
    :mod:`kdive.providers.shared.build_timeouts`, which scale by this same multiplier. Two
    in-guest ``build-fs`` runs spent **2406 s / 2888 s** in ``virt-tar-out`` and
    **2032 s / 2293 s** in ``virt-make-fs``, all exiting 0. Every figure exceeds the unscaled
    1800 s base, so scaling is required rather than precautionary, and all sit well inside the
    scaled 18000 s — the largest at 1.60x the base against a 10x multiplier.

    **The headroom is not uniform across stages.** The ``guestfish`` normalization step, whose
    base is 300 s rather than 1800 s, took **1365 s** — **4.55x** its base. Do not read the
    1.1-1.6x figures above as the multiplier's working range: they belong to the two longest
    bases, where the fixed appliance-boot cost is amortized over more work. A smaller base pays
    the same boot and so asks a larger ratio, which is what puts a 300 s budget four-and-a-half
    times under water on this host. The 10x ADR-0341 hands down still covers every measurement
    taken, but with roughly 2x margin at the tightest stage rather than the ~6x the long-base
    figures alone would imply.

    The timings, their sampling error, and what the runs did *not* establish are recorded in
    ``docs/design/2026-09-09-ppc64le-emulated-power-live-proof-2383-proof-record.md``.

    ``kvm_present`` is injected so both branches are unit-tested without a real ``/dev/kvm``.
    """
    probe = kvm_present if kvm_present is not None else kvm_probe_for_uri(resolved_libvirt_uri())
    return tcg_deadline_multiplier("kvm" if probe() else None)
