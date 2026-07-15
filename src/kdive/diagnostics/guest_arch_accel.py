"""The guest-arch-accelerator worker-vantage diagnostic contribution + probe (ADR-0352, #1153).

Cross-arch guests run under KVM (native arch) or TCG (foreign arch), and that accelerator is
invisible to an operator until a guest boots slowly. This contribution adds one worker-vantage
check that reports, per schedulable guest arch, KVM-native vs TCG-only — probing the worker host
directly (PATH for the qemu emulators + the URI-selected ``/dev/kvm`` signal), so it needs no DB
handle and cannot diverge from a stale inventory (ADR-0091). It attributes to ``local-libvirt``
(the provider that runs the guests) and rides the single local diagnostic contribution alongside
``multiarch_gdb`` and ``pseries_fadump``.

Unlike those two, accel is genuinely URI-dependent, so the probe reads the libvirt connection URI
to select the KVM probe: ``/dev/kvm`` *presence* under ``qemu:///system`` (qemu runs privileged),
worker-uid *openability* under ``qemu:///session`` (qemu runs as the worker). It reads the URI from
the config ``env_snapshot`` by variable name — not by importing the local-libvirt settings — so it
respects the provider boundary (only ``composition.py`` may import ``providers.local_libvirt.*``),
and stays faithful to the same ``KDIVE_*`` snapshot the registry resolves against (the pattern the
``secret_ref`` check uses).
"""

from __future__ import annotations

import os
import platform
import shutil
from collections.abc import Callable

from kdive.config import env_snapshot
from kdive.diagnostics.checks import GUEST_ARCH_ACCEL_ID, Check
from kdive.diagnostics.provider_checks import (
    GuestArchAccelCheck,
    GuestArchAccelProbe,
    GuestArchAccelReport,
)
from kdive.diagnostics.provider_contracts import WorkerVantageDescriptor
from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES

_LOCAL_PROVIDER = "local-libvirt"
_KVM_NODE = "/dev/kvm"
_SESSION_URI = "qemu:///session"
_DEFAULT_URI = "qemu:///system"
# The libvirt connection-URI env var (mirrors local_libvirt.settings.LIBVIRT_URI.name / default).
# Read by name from the config snapshot to avoid importing the boundary-gated provider settings.
_LIBVIRT_URI_ENV = "KDIVE_LIBVIRT_URI"

# The qemu system-emulator binary per supported arch. Asymmetric and NOT ``uname -m``: ppc64le
# maps to ``qemu-system-ppc64`` (POWER has no ``-ppc64le`` binary). One entry per SUPPORTED_ARCHES
# row; a future arch adds a row here alongside its arch_traits entry.
_QEMU_SYSTEM_BINARY: dict[str, str] = {
    "x86_64": "qemu-system-x86_64",
    "ppc64le": "qemu-system-ppc64",
}


def qemu_system_binary(arch: str) -> str | None:
    """Return the qemu system-emulator binary for a supported arch, or ``None`` if unknown."""
    return _QEMU_SYSTEM_BINARY.get(arch)


def resolved_libvirt_uri() -> str:
    """Return the configured libvirt URI from the ``KDIVE_*`` snapshot, defaulting to system.

    Reads the URI by variable name from :func:`kdive.config.env_snapshot` (not by importing the
    boundary-gated provider settings), matching the same snapshot the registry resolves against.
    """
    return env_snapshot().get(_LIBVIRT_URI_ENV, _DEFAULT_URI)


def kvm_probe_for_uri(
    uri: str,
    *,
    node: str = _KVM_NODE,
    access: Callable[[str, int], bool] = os.access,
    exists: Callable[[str], bool] = os.path.exists,
) -> Callable[[], bool]:
    """Build the URI-selected host-KVM probe (ADR-0352).

    ``qemu:///session`` runs qemu as the worker uid, so the signal is worker-uid *openability*
    (``os.access`` R+W). Any other URI — the default ``qemu:///system`` and privileged/remote
    URIs — runs qemu privileged, so the signal is ``/dev/kvm`` *presence* (``os.path.exists``),
    which succeeds regardless of the worker uid. ``node``/``access``/``exists`` are injected so
    the branch is unit-tested without touching the real ``/dev/kvm``.
    """
    if uri.strip() == _SESSION_URI:
        return lambda: access(node, os.R_OK | os.W_OK)
    return lambda: exists(node)


def default_guest_arch_accel_probe(
    *,
    host_arch: str | None = None,
    supported: frozenset[str] = SUPPORTED_ARCHES,
    which: Callable[[str], str | None] = shutil.which,
    kvm_present: Callable[[], bool] | None = None,
) -> GuestArchAccelProbe:
    """Build the probe that observes the per-arch guest accelerator on the worker host.

    ``host_arch``/``supported``/``which``/``kvm_present`` are injected (defaults are the real host,
    ``arch_traits.SUPPORTED_ARCHES``, ``shutil.which``, and the URI-selected ``/dev/kvm`` probe) so
    the probe is unit-tested with no real host. When ``kvm_present`` is ``None`` the URI is resolved
    at call time from the config snapshot (defaulting to ``qemu:///system`` when unset) — call-time
    resolution sidesteps config-load ordering.
    """
    resolved_host = host_arch if host_arch is not None else platform.machine()

    async def _probe() -> GuestArchAccelReport:
        kvm = kvm_present
        if kvm is None:
            kvm = kvm_probe_for_uri(resolved_libvirt_uri())
        accel_by_arch: dict[str, str] = {}
        for arch in sorted(supported):
            binary = qemu_system_binary(arch)
            if binary is None or which(binary) is None:
                continue
            accel_by_arch[arch] = "kvm" if arch == resolved_host and kvm() else "tcg"
        native_binary = qemu_system_binary(resolved_host)
        native_present = native_binary is not None and which(native_binary) is not None
        return GuestArchAccelReport(
            accel_by_arch=accel_by_arch,
            native_arch=resolved_host,
            native_supported=resolved_host in supported,
            native_emulator_present=native_present,
            native_qemu_binary=native_binary,
        )

    return _probe


def guest_arch_accel_worker_check() -> Check:
    """The guest-arch-accel worker-vantage check, for the single local-libvirt contribution."""
    return GuestArchAccelCheck(provider=_LOCAL_PROVIDER, probe=default_guest_arch_accel_probe())


def guest_arch_accel_worker_descriptor() -> WorkerVantageDescriptor:
    """The worker-check descriptor surfaced when the worker vantage is unavailable."""
    return WorkerVantageDescriptor(id=GUEST_ARCH_ACCEL_ID, provider=_LOCAL_PROVIDER)
