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
from kdive.diagnostics.contributions.guest_arch_accel import kvm_probe_for_uri
from kdive.providers.local_libvirt.settings import LIBVIRT_TCG_DEADLINE_MULTIPLIER

#: The unscaled budget for one slow rootfs build tool on a worker host with usable KVM.
SLOW_BUILD_TOOL_TIMEOUT_S = 30 * 60


def appliance_budget_s(base_s: int, *, kvm_present: Callable[[], bool] | None = None) -> int:
    """Scale one libguestfs appliance budget by the worker host's KVM (#2397, ADR-0648).

    Every budget this scales bounds a tool that boots the *same* host-arch appliance, so they all
    move by one factor from one probe. A host whose probe reports usable KVM is unscaled and keeps
    exactly today's figure. A host that cannot open the KVM node emulates the appliance kernel and
    scales by ``KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER`` — the same knob ADR-0636 applied to the
    ``virt-customize`` budget, so an operator still moves every appliance budget together.

    ``base_s`` is a per-tool figure rather than one shared constant because the tools are not
    alike: repacking a whole disk is bounded at :data:`SLOW_BUILD_TOOL_TIMEOUT_S`, while a
    read-only ``guestfish`` marker read or a ``virt-inspector`` pass is bounded at five minutes.
    What they share is the appliance boot, which is what emulation makes expensive.

    The result is an ``int`` because ``run_guestfs_tool`` takes ``timeout_s: int`` and echoes it
    into the timeout error's ``details`` payload. ``kvm_present`` is injected so both branches are
    unit-tested without a real ``/dev/kvm``; the shared openability probe runs per call, so it
    answers for the host as it is when the tool runs.
    """
    probe = kvm_present if kvm_present is not None else kvm_probe_for_uri("")
    if probe():
        return base_s
    return int(base_s * config.require(LIBVIRT_TCG_DEADLINE_MULTIPLIER))


def slow_build_tool_timeout_s(*, kvm_present: Callable[[], bool] | None = None) -> int:
    """Return :data:`SLOW_BUILD_TOOL_TIMEOUT_S` scaled by the worker host's KVM (#2397).

    Measured for #2383 on an emulated-POWER host, in-guest ``build-fs`` failed at
    ``virt-tar-out exceeded its timeout {'timeout_s': 1800}``, a budget that host could not meet.
    Measured again for #2414 once the probe read that host correctly, the same stage completed in
    2406 s and ``virt-make-fs`` in 2032 s — both above the unscaled base, both inside the scaled
    budget.
    """
    return appliance_budget_s(SLOW_BUILD_TOOL_TIMEOUT_S, kvm_present=kvm_present)
