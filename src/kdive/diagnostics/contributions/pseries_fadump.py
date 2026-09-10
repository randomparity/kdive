"""The pseries-fadump worker-vantage diagnostic contribution (ADR-0349, #1151).

fadump on POWER pseries needs a host QEMU >= 10.2 (the ``ibm,configure-kernel-dump`` RTAS). This
contribution adds one worker-vantage check that resolves ``qemu-system-ppc64`` — on PATH, or at
the RedHat off-PATH location on a ppc64le host (ADR-0636) — and compares its version against the
floor — reusing :func:`detect_pseries_fadump` — so it needs no DB handle
and no libvirt call. The synchronous version detector runs in a worker thread so the shared
per-check timeout can interrupt the async probe. It attributes to ``local-libvirt`` (the provider
that runs ppc64le guests) but depends on no local-libvirt internals, so it lives in the neutral
diagnostics package.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
from collections.abc import Callable

from kdive.diagnostics.checks import PSERIES_FADUMP_ID, Check
from kdive.diagnostics.provider_checks import (
    PseriesFadumpCheck,
    PseriesFadumpOutcome,
    PseriesFadumpProbe,
)
from kdive.diagnostics.provider_contracts import WorkerVantageDescriptor
from kdive.providers.shared.fadump_detect import (
    VersionRunner,
    detect_pseries_fadump,
)

_LOCAL_PROVIDER = "local-libvirt"
_PPC64_EMULATOR = "qemu-system-ppc64"
_PPC64_HOST = "ppc64le"
# The RedHat family ships the host's OWN emulator here, off PATH: no EL package provides
# /usr/bin/qemu-system-<arch> (ADR-0636). A PATH-only probe therefore reports not_applicable on
# an EL ppc64le host — the exact host fadump exists for — while LocalLibvirtDiscovery reads
# libvirt's capabilities XML and sees the arch, so doctor and discovery diverge. Native arch
# only: /usr/libexec/qemu-kvm is this host's emulator, never a foreign-arch one.
_LIBEXEC_EMULATOR = "/usr/libexec/qemu-kvm"


def _is_executable(path: str) -> bool:
    """Whether ``path`` is an executable *file* (a directory is executable but not an emulator)."""
    return os.path.isfile(path) and os.access(path, os.X_OK)


def default_pseries_fadump_probe(
    *,
    which: Callable[[str], str | None] = shutil.which,
    run_version: VersionRunner | None = None,
    host_arch: str | None = None,
    libexec_emulator: str = _LIBEXEC_EMULATOR,
    is_executable: Callable[[str], bool] = _is_executable,
) -> PseriesFadumpProbe:
    """Build the probe that decides whether the host QEMU implements pseries fadump.

    ``which``, ``run_version``, ``host_arch`` and ``is_executable`` are injected (defaults are
    ``shutil.which``, :func:`detect_pseries_fadump`'s own bounded subprocess, ``platform.machine``
    and an executable-file test) so the probe is unit-tested without a real qemu. The emulator is
    resolved rather than PATH-probed: PATH first, then — on a ppc64le host only — the RedHat
    off-PATH location (ADR-0636). A host with no ppc64 emulator at all cannot run ppc64le guests,
    so fadump is ``not_applicable`` and no subprocess is spawned; otherwise the emulator's version
    is compared against the floor via the same :func:`detect_pseries_fadump` discovery uses, so
    doctor and discovery cannot diverge. The detector is offloaded so the async probe yields while
    its synchronous version subprocess runs and :func:`run_check` can enforce its deadline.
    """
    resolved_host = host_arch if host_arch is not None else platform.machine()

    async def _probe() -> PseriesFadumpOutcome:
        emulator = which(_PPC64_EMULATOR)
        if emulator is None and resolved_host == _PPC64_HOST and is_executable(libexec_emulator):
            emulator = libexec_emulator
        if emulator is None:
            return PseriesFadumpOutcome.NOT_APPLICABLE
        arches = {"ppc64le": {"accel": "tcg", "emulator": emulator}}
        supported = (
            await asyncio.to_thread(detect_pseries_fadump, arches)
            if run_version is None
            else await asyncio.to_thread(detect_pseries_fadump, arches, run_version=run_version)
        )
        return PseriesFadumpOutcome.SUPPORTED if supported else PseriesFadumpOutcome.UNSUPPORTED

    return _probe


def pseries_fadump_worker_check() -> Check:
    """The fadump worker-vantage check, for the single local-libvirt diagnostic contribution."""
    return PseriesFadumpCheck(provider=_LOCAL_PROVIDER, probe=default_pseries_fadump_probe())


def pseries_fadump_worker_descriptor() -> WorkerVantageDescriptor:
    """The fadump worker-check descriptor surfaced when the worker vantage is unavailable."""
    return WorkerVantageDescriptor(id=PSERIES_FADUMP_ID, provider=_LOCAL_PROVIDER)
