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
    """Whether this worker's uid can open the KVM node read+write (ADR-0637, #2397).

    This is the appliance's own question and it has one answer, so it is asked one way: the
    libguestfs appliance is started by the worker process itself under
    ``LIBGUESTFS_BACKEND=direct`` (``deploy/ansible/playbooks/image.yml``), never by libvirtd, so
    what decides KVM-versus-emulation is whether *this uid* can open the node — regardless of the
    libvirt connection URI.

    That is the definition the repository's shell tier already enforces for the same appliance:
    ``scripts/live-vm/preflight-env.sh`` dies when ``${KDIVE_KVM_NODE:-/dev/kvm}`` is not readable
    **and** writable by the running user, with the comment "without KVM the libguestfs appliance
    falls back to emulation"; ``scripts/operations/check-local-libvirt.sh`` and
    ``scripts/check-setup-deps.sh`` use the same test. Honouring ``KDIVE_KVM_NODE``, and treating
    it as unset when empty, keeps the two tiers answering identically for one host.

    It deliberately diverges from ADR-0352's ``kvm_probe_for_uri``, which the sibling
    ``virt-customize`` budget uses: that probe tests mere *presence* for every URI but
    ``qemu:///session``, so a worker uid that cannot open the node reads as KVM. ADR-0637 records
    why the two appliance budgets differ, and converging them by widening ADR-0352's probe is a
    filed follow-up.
    """
    node = config.env_snapshot().get(_KVM_NODE_ENV) or _DEFAULT_KVM_NODE
    return os.access(node, os.R_OK | os.W_OK)


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
