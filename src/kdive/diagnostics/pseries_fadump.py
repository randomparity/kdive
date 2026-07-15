"""The pseries-fadump worker-vantage diagnostic contribution (ADR-0349, #1151).

fadump on POWER pseries needs a host QEMU >= 10.2 (the ``ibm,configure-kernel-dump`` RTAS). This
contribution adds one worker-vantage check that finds ``qemu-system-ppc64`` on PATH and compares
its version against the floor — reusing :func:`detect_pseries_fadump` — so it needs no DB handle
and no libvirt call. It attributes to ``local-libvirt`` (the provider that runs ppc64le guests)
but depends on no local-libvirt internals, so it lives in the neutral diagnostics package.
"""

from __future__ import annotations

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


def default_pseries_fadump_probe(
    *,
    which: Callable[[str], str | None] = shutil.which,
    run_version: VersionRunner | None = None,
) -> PseriesFadumpProbe:
    """Build the probe that decides whether the host QEMU implements pseries fadump.

    ``which`` and ``run_version`` are injected (defaults are ``shutil.which`` and
    :func:`detect_pseries_fadump`'s own bounded subprocess) so the probe is unit-tested without a
    real qemu. A host with no ``qemu-system-ppc64`` cannot run ppc64le guests, so fadump is
    ``not_applicable`` and no subprocess is spawned; otherwise the emulator's version is compared
    against the floor via the same :func:`detect_pseries_fadump` discovery uses, so doctor and
    discovery cannot diverge.
    """

    async def _probe() -> PseriesFadumpOutcome:
        emulator = which(_PPC64_EMULATOR)
        if emulator is None:
            return PseriesFadumpOutcome.NOT_APPLICABLE
        arches = {"ppc64le": {"accel": "tcg", "emulator": emulator}}
        supported = (
            detect_pseries_fadump(arches)
            if run_version is None
            else detect_pseries_fadump(arches, run_version=run_version)
        )
        return PseriesFadumpOutcome.SUPPORTED if supported else PseriesFadumpOutcome.UNSUPPORTED

    return _probe


def pseries_fadump_worker_check() -> Check:
    """The fadump worker-vantage check, for the single local-libvirt diagnostic contribution."""
    return PseriesFadumpCheck(provider=_LOCAL_PROVIDER, probe=default_pseries_fadump_probe())


def pseries_fadump_worker_descriptor() -> WorkerVantageDescriptor:
    """The fadump worker-check descriptor surfaced when the worker vantage is unavailable."""
    return WorkerVantageDescriptor(id=PSERIES_FADUMP_ID, provider=_LOCAL_PROVIDER)
