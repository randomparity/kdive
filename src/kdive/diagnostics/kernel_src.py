"""The production local warm-tree source probe adapter (ADR-0163, #533/#532).

The build-host boundary for :class:`~kdive.diagnostics.checks.LocalKernelSrcCheck`: it resolves
``KDIVE_KERNEL_SRC`` from the config snapshot and classifies it over the single shared
``warm_tree_source_error`` predicate (``services/runs/build_host_policy.py``) — the same rule
the build-time ``sync_tree`` and the admission-time ``check_warm_tree_source_admission`` enforce
(ADR-0161).

``KDIVE_KERNEL_SRC`` resolution is deferred to probe time (mirroring ``reachability.py``): the
``config.get`` snapshot read happens when the check runs, so a value that drifts after assembly is
reflected in the verdict rather than frozen at factory time. ``config.get`` reads the snapshot
regardless of the setting's ``processes=_WORKER`` tag (``processes`` only gates startup
``validate()``), so the server process can read it. The unset-vs-invalid split reuses the
predicate's own return values — the single rule — rather than re-deriving it. The lone
``Path.is_dir()`` stat the predicate performs is cheap and synchronous, so unlike the libvirt
probes there is no blocking RPC to offload with :func:`asyncio.to_thread`.
"""

from __future__ import annotations

from collections.abc import Callable

import kdive.config as config
from kdive.config.core_settings import KERNEL_SRC
from kdive.diagnostics.checks import WarmTreeSourceOutcome, WarmTreeSourceProbe
from kdive.services.runs.build_host_policy import (
    KERNEL_SRC_UNSET_DETAIL,
    warm_tree_source_error,
)


def _kernel_src_from_config() -> str:
    """Resolve ``KDIVE_KERNEL_SRC`` from the config snapshot (``""`` when unset)."""
    return config.get(KERNEL_SRC) or ""


def warm_tree_source_probe(
    *, source: Callable[[], str] = _kernel_src_from_config
) -> WarmTreeSourceProbe:
    """Build the async warm-tree source probe over the injected ``source``.

    Args:
        source: Resolves the ``KDIVE_KERNEL_SRC`` value (production: the config snapshot read;
            tests inject a fixed value). Called at probe time, not factory time.

    Returns:
        An async, no-arg probe returning a :class:`WarmTreeSourceOutcome`.
    """

    async def probe() -> WarmTreeSourceOutcome:
        error = warm_tree_source_error(source())
        if error is None:
            return WarmTreeSourceOutcome.USABLE
        if error == KERNEL_SRC_UNSET_DETAIL:
            return WarmTreeSourceOutcome.UNSET
        return WarmTreeSourceOutcome.INVALID

    return probe
