"""Shared provider build subprocess timeouts (ADR-0636, ADR-0637).

The rootfs build tools drive a libguestfs appliance, which is a *host-arch* VM the worker boots
on its own host to edit a disk — so what sets their speed is whether the worker host has usable
KVM, not any System's accelerator. :func:`slow_build_tool_timeout_s` resolves that here rather
than importing ``local_libvirt``'s equivalent helper, because ``providers/shared`` may not reach
into a concrete provider package: ``tests/providers/test_provider_boundaries.py`` allows only
``local_libvirt.settings``, the ADR-0087 declaration module (ADR-0637, #2397).
"""

from __future__ import annotations

from collections.abc import Callable

import kdive.config as config
from kdive.diagnostics.contributions.guest_arch_accel import (
    kvm_probe_for_uri,
    resolved_libvirt_uri,
)
from kdive.providers.local_libvirt.settings import LIBVIRT_TCG_DEADLINE_MULTIPLIER

#: The unscaled budget for one slow rootfs build tool on a worker host with KVM.
SLOW_BUILD_TOOL_TIMEOUT_S = 30 * 60


def slow_build_tool_timeout_s(*, kvm_present: Callable[[], bool] | None = None) -> int:
    """Return :data:`SLOW_BUILD_TOOL_TIMEOUT_S` scaled by the worker host's KVM (#2397).

    A host whose probe reports KVM is unscaled, so the fast path keeps exactly today's 1800 s.
    A host with no ``/dev/kvm`` emulates the appliance kernel and scales by
    ``KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER`` — the same knob ADR-0636 applied to the
    ``virt-customize`` budget, so an operator still moves every appliance budget together.
    Measured for #2383 on an emulated-POWER host, in-guest ``build-fs`` failed at
    ``virt-tar-out exceeded its timeout {'timeout_s': 1800}``, a budget that host could not meet.

    The result is an ``int`` because ``run_guestfs_tool`` takes ``timeout_s: int`` and echoes it
    into the timeout error's ``details`` payload. ``kvm_present`` is injected so both branches are
    unit-tested without a real ``/dev/kvm``; the default probe is resolved per call, so it answers
    for the host as it is when the tool runs (ADR-0352).

    The probe's reach is ADR-0352's, not this function's: for the default ``qemu:///system`` it
    tests ``/dev/kvm`` *presence*, so a host that has the node but advertises no KVM domain for
    its architecture — the POWER10 host recorded in ``tests/providers/test_libvirt_xml.py`` —
    reads as KVM here and keeps the unscaled budget even though its appliance is emulated.
    """
    probe = kvm_present if kvm_present is not None else kvm_probe_for_uri(resolved_libvirt_uri())
    if probe():
        return SLOW_BUILD_TOOL_TIMEOUT_S
    return int(SLOW_BUILD_TOOL_TIMEOUT_S * config.require(LIBVIRT_TCG_DEADLINE_MULTIPLIER))
