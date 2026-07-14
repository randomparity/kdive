"""The multiarch-gdb worker-vantage diagnostic contribution (ADR-0347).

Cross-arch debug sessions (a ppc64le guest on an x86_64 host) spawn a multiarch-capable gdb on
the worker host. This contribution adds one worker-vantage check that confirms such a gdb exists,
keyed off kdive's *static* cross-arch capability (``SUPPORTED_ARCHES − host``) — so the probe
needs no DB handle and no libvirt call — and gdb's *stdout* (its batch exit status is unreliable
for a rejected ``set architecture``). It attributes to ``local-libvirt`` (the provider that runs
cross-arch guests) but depends on no local-libvirt internals, so it lives in the neutral
diagnostics package rather than behind the provider-assembly seam.
"""

from __future__ import annotations

import asyncio
import contextlib
import platform
import shutil
from collections.abc import Awaitable, Callable

from kdive.diagnostics.checks import MULTIARCH_GDB_ID, Check
from kdive.diagnostics.provider_checks import (
    MultiarchGdbCheck,
    MultiarchGdbOutcome,
    MultiarchGdbProbe,
)
from kdive.diagnostics.provider_contracts import (
    DiagnosticProviderContribution,
    WorkerVantageDescriptor,
    no_checks,
)
from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES
from kdive.providers.shared.debug_common.gdbmi.policy.arch import (
    gdb_target_arch_name,
    select_gdb_binary,
)

_LOCAL_PROVIDER = "local-libvirt"

# Per-invocation budget for one `gdb --batch set architecture` probe. Kept well under the
# diagnostics worker handler's 6s per-check timeout so the whole probe (one gdb run per supported
# foreign arch) finishes inside that budget rather than being cut off as an ERROR. A gdb that
# cannot answer within this budget is reported as "undeterminable" (a hung/broken gdb), distinct
# from "missing" (an uninstalled one).
_GDB_PROBE_TIMEOUT_SEC = 2.0

# Type of the injected batch runner: given argv, return gdb's stdout; raise on spawn/timeout.
GdbBatchRunner = Callable[[list[str]], Awaitable[str]]


async def _run_gdb_batch(argv: list[str]) -> str:
    """Run a gdb batch invocation and return its stdout; raise ``OSError``/``TimeoutError``.

    A non-zero exit is *not* treated as failure here — the caller decides on stdout, because gdb's
    batch exit status does not reliably reflect a rejected ``set architecture``.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_GDB_PROBE_TIMEOUT_SEC)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        raise
    return stdout.decode("utf-8", errors="replace")


def default_multiarch_gdb_probe(
    *,
    host_arch: str | None = None,
    supported: frozenset[str] = SUPPORTED_ARCHES,
    which: Callable[[str], str | None] = shutil.which,
    run: GdbBatchRunner = _run_gdb_batch,
) -> MultiarchGdbProbe:
    """Build the probe that decides whether every supported foreign arch is gdb-targetable.

    ``host_arch``/``supported``/``which``/``run`` are injected (defaults are the real host,
    ``arch_traits.SUPPORTED_ARCHES``, ``shutil.which``, and a bounded subprocess) so the probe is
    unit-tested without a real gdb. The foreign set is ``supported − {host_arch}``; a host whose
    only supported arch is its own reports ``SUPPORTED`` without spawning anything.
    """
    resolved_host = host_arch if host_arch is not None else platform.machine()

    async def _probe() -> MultiarchGdbOutcome:
        foreign = sorted(set(supported) - {resolved_host})
        for arch in foreign:
            candidate = select_gdb_binary(resolved_host, arch, which)
            if candidate is None:
                return MultiarchGdbOutcome.MISSING
            gdb_name = gdb_target_arch_name(arch)
            if gdb_name is None:
                # No gdb architecture name for a supported arch: cannot positively confirm, so
                # do not claim support. Unreachable for the current SUPPORTED_ARCHES.
                return MultiarchGdbOutcome.MISSING
            try:
                stdout = await run(
                    [
                        candidate,
                        "--batch",
                        "-nx",
                        "-ex",
                        f"set architecture {gdb_name}",
                        "-ex",
                        "show architecture",
                    ]
                )
            except OSError, TimeoutError:
                return MultiarchGdbOutcome.UNDETERMINABLE
            if gdb_name not in stdout:
                return MultiarchGdbOutcome.MISSING
        return MultiarchGdbOutcome.SUPPORTED

    return _probe


def _worker_checks() -> list[Check]:
    return [MultiarchGdbCheck(provider=_LOCAL_PROVIDER, probe=default_multiarch_gdb_probe())]


def _unavailable_worker_checks() -> list[WorkerVantageDescriptor]:
    return [WorkerVantageDescriptor(id=MULTIARCH_GDB_ID, provider=_LOCAL_PROVIDER)]


def diagnostic_contribution() -> DiagnosticProviderContribution:
    return DiagnosticProviderContribution(
        provider=_LOCAL_PROVIDER,
        enabled=lambda: True,
        checks=no_checks,
        unavailable_worker_checks=_unavailable_worker_checks,
        worker_checks=_worker_checks,
    )
