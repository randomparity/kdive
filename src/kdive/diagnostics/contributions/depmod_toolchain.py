"""The module-staging toolchain worker-vantage diagnostic contribution (ADR-0635, #2339).

Without this check, a ``depmod`` outside the four explicit directories (#2300) is first reported
mid-install, after a System has been allocated and a guest booted. The probe reads the same
contract the install path resolves against — from ``kdive.providers.shared``, the way
``pseries_fadump`` and ``multiarch_gdb`` read theirs — so the verdict cannot name directories the
run will not search. It attributes to ``local-libvirt`` and rides that provider's single
contribution.
"""

from __future__ import annotations

import shutil
from typing import Protocol

from kdive.diagnostics.checks import DEPMOD_TOOLCHAIN_ID, Check
from kdive.diagnostics.provider_checks import DepmodToolchainCheck, DepmodToolchainProbe
from kdive.diagnostics.provider_contracts import WorkerVantageDescriptor
from kdive.providers.shared.module_staging_tools import DEPMOD, DEPMOD_SEARCH_PATH

_LOCAL_PROVIDER = "local-libvirt"


class ToolResolver(Protocol):
    """Resolve a binary name against an explicit search path, never ``PATH``.

    ``shutil.which``'s second positional parameter is ``mode``, so the path is always keyword.
    """

    def __call__(self, cmd: str, *, path: str) -> str | None: ...


def default_depmod_toolchain_probe(
    *,
    which: ToolResolver = shutil.which,
) -> DepmodToolchainProbe:
    """Build the probe that resolves ``depmod`` against the module-staging search contract.

    ``which`` is injected (default :func:`shutil.which`) so the probe is unit-tested without
    depending on the host's layout. Passing ``DEPMOD_SEARCH_PATH`` explicitly keeps ``PATH`` — and
    its ``os.defpath`` fallback — out of the resolution, the defect #2300 fixed.
    """

    async def _probe() -> str | None:
        return which(DEPMOD, path=DEPMOD_SEARCH_PATH)

    return _probe


def depmod_toolchain_worker_check() -> Check:
    """The module-staging toolchain check, for the single local-libvirt contribution."""
    return DepmodToolchainCheck(provider=_LOCAL_PROVIDER, probe=default_depmod_toolchain_probe())


def depmod_toolchain_worker_descriptor() -> WorkerVantageDescriptor:
    """The toolchain worker-check descriptor surfaced when the worker vantage is unavailable."""
    return WorkerVantageDescriptor(id=DEPMOD_TOOLCHAIN_ID, provider=_LOCAL_PROVIDER)
