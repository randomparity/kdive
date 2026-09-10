"""Shared provider build subprocess timeouts (ADR-0636, ADR-0637).

The rootfs build tools drive a libguestfs appliance, which is a *host-arch* VM the worker boots
on its own host to edit a disk — so what sets their speed is whether the worker host has usable
KVM, not any System's accelerator. :func:`slow_build_tool_timeout_s` resolves that here rather
than importing ``local_libvirt``'s equivalent helper, because ``providers/shared`` may not reach
into a concrete provider package: ``tests/providers/test_provider_boundaries.py`` allows only
``local_libvirt.settings``, the ADR-0087 declaration module (ADR-0637, #2397).
"""

from __future__ import annotations

import os
from collections.abc import Callable

import kdive.config as config
from kdive.providers.local_libvirt.settings import LIBVIRT_TCG_DEADLINE_MULTIPLIER

#: The unscaled budget for one slow rootfs build tool on a worker host with usable KVM.
SLOW_BUILD_TOOL_TIMEOUT_S = 30 * 60

# The KVM device node the appliance would open. A catalogued non-registry variable
# (``kdive.config.external_env``) read by name from the config snapshot, which is how a
# ``KDIVE_*`` name with no Setting is read without diverging from what the registry sees.
_KVM_NODE_ENV = "KDIVE_KVM_NODE"
_DEFAULT_KVM_NODE = "/dev/kvm"


def _worker_host_kvm_usable() -> bool:
    """Whether the KVM node actually opens read+write, so the appliance gets KVM (ADR-0637, #2397).

    The URI is not the right selector for this appliance. The tools this budget bounds are run as
    a fixed argv by ``images/planes/_build_common.run_guestfs_tool`` with no ``-c`` and no
    environment override, so ``KDIVE_LIBVIRT_URI`` — the variable ADR-0352's probe keys on — never
    reaches them and does not describe how libguestfs launches its appliance.

    **The node is opened, not stat-ed.** ``os.access`` tests permission bits, and on a systemd host
    the bits say nothing about whether KVM exists: ``50-udev-default.rules`` carries
    ``KERNEL=="kvm", MODE="0666", OPTIONS+="static_node=kvm"``, so ``/dev/kvm`` is created at 0666
    during boot whether or not ``kvm`` ever loads — that is what ``static_node`` is *for*, since
    opening the node is what triggers module autoload. A host with the module blacklisted therefore
    passes an ``access`` test and fails the ``open`` with ``ENODEV``. Measured on the #2383
    emulated-POWER host of record (Fedora 44 ppc64le, ``blacklist kvm`` +
    ``install kvm /bin/false``, no ``kvm`` module loaded, libvirt advertising zero KVM domains):
    ``os.access`` returned ``True`` while ``os.open`` raised ``OSError 19 ENODEV``. Under the
    permission test this budget stayed at the unscaled 1800 s on the one host class #2397 exists to
    scale, so the fix never engaged there (#2414).

    Opening is also what the consumer does: the libguestfs appliance opens the node, so a probe
    that opens it answers the question the budget is asking. Autoload is the intended side effect —
    a host where the module *can* load has KVM and should read as KVM — and the descriptor is
    closed immediately, since creating a VM needs a further ``KVM_CREATE_VM`` ioctl this never
    issues. A missing node raises ``FileNotFoundError``, an ``OSError`` like any other, so it is the
    same answer by the same path.

    ``KDIVE_KVM_NODE`` is honoured, and an empty value is treated as unset, as ``${…:-…}`` does.

    It deliberately diverges from ADR-0352's ``kvm_probe_for_uri``, which the sibling
    ``virt-customize`` budget uses: that probe tests mere *presence* for every URI but
    ``qemu:///session``. It now also diverges from the repository's **shell** tier, which still
    tests permission — ``scripts/live-vm/preflight-env.sh``,
    ``scripts/operations/check-local-libvirt.sh`` and ``scripts/check-setup-deps.sh`` all use
    ``[ -r ]``/``[ -w ]``, i.e. ``access(2)`` — so on a static-node host those three still call KVM
    present where this returns ``False``. Converging all of them onto one openability test is
    #2410's job; this fixes the budget that silently took the wrong branch.
    """
    node = config.env_snapshot().get(_KVM_NODE_ENV) or _DEFAULT_KVM_NODE
    try:
        fd = os.open(node, os.O_RDWR)
    except OSError:
        return False
    os.close(fd)
    return True


def slow_build_tool_timeout_s(*, kvm_present: Callable[[], bool] | None = None) -> int:
    """Return :data:`SLOW_BUILD_TOOL_TIMEOUT_S` scaled by the worker host's KVM (#2397).

    A host whose probe reports usable KVM is unscaled, so the fast path keeps exactly today's
    1800 s. A host that cannot open the KVM node emulates the appliance kernel and scales by
    ``KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER`` — the same knob ADR-0636 applied to the
    ``virt-customize`` budget, so an operator still moves every appliance budget together.
    Measured for #2383 on an emulated-POWER host, in-guest ``build-fs`` failed at
    ``virt-tar-out exceeded its timeout {'timeout_s': 1800}``, a budget that host could not meet.

    The result is an ``int`` because ``run_guestfs_tool`` takes ``timeout_s: int`` and echoes it
    into the timeout error's ``details`` payload. ``kvm_present`` is injected so both branches are
    unit-tested without a real ``/dev/kvm``; the default probe
    (:func:`_worker_host_kvm_usable`) runs per call, so it answers for the host as it is when the
    tool runs.
    """
    probe = kvm_present if kvm_present is not None else _worker_host_kvm_usable
    if probe():
        return SLOW_BUILD_TOOL_TIMEOUT_S
    return int(SLOW_BUILD_TOOL_TIMEOUT_S * config.require(LIBVIRT_TCG_DEADLINE_MULTIPLIER))
