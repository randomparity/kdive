"""The production local warm-tree source probe adapter (ADR-0163, #533/#532).

The build-host boundary for :class:`~kdive.diagnostics.checks.LocalKernelSrcCheck`: it resolves
``KDIVE_KERNEL_SRC`` from the config snapshot and classifies it over the single shared
``warm_tree_source_error`` predicate (``db/build_host_policy.py``) — the same rule
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

import asyncio
import logging
import subprocess  # noqa: S404 - fixed argv, no shell, best-effort git HEAD read (#845)
from collections.abc import Awaitable, Callable

from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import KERNEL_SRC
from kdive.db.build_host_policy import (
    KERNEL_SRC_UNSET_DETAIL,
    warm_tree_source_error,
)
from kdive.db.build_hosts import WORKER_LOCAL_ID, get_by_id
from kdive.diagnostics.checks import (
    WarmTreeSourceOutcome,
    WarmTreeSourceProbe,
    WarmTreeSourceProbeResult,
)

_log = logging.getLogger(__name__)

# The git HEAD short-commit length disclosed in CheckResult.data (the 12-char prefix of the full
# SHA `_rev_parse_head` returns) — long enough to be unambiguous, short enough to read (#845).
_SHORT_COMMIT_LEN = 12
# The bound on the whole best-effort git read (commit + branch), kept well under the diagnostics
# per-check budget (10s, `checks.py` `run_check`) so a slow/hung tree leaves the git fields unset
# and the verdict stays USABLE rather than timing the check out to ERROR (#845).
_GIT_READ_TIMEOUT = 5.0

# The injectable git reader: `(tree) -> (short_commit, branch)`, so tests drive the probe's data
# path without a real checkout (#845).
GitHeadReader = Callable[[str], tuple[str | None, str | None]]


def _kernel_src_from_config() -> str:
    """Resolve ``KDIVE_KERNEL_SRC`` from the config snapshot (``""`` when unset)."""
    return config.get(KERNEL_SRC) or ""


def _rev_parse_branch(tree: str) -> str | None:
    """Return the current branch (``git -C <tree> rev-parse --abbrev-ref HEAD``), or ``None``.

    Best-effort, mirroring ``dispatch._rev_parse_head``: any failure (not a git tree, ``git``
    absent) or a detached HEAD (``--abbrev-ref`` prints ``HEAD``) yields ``None`` — there is no
    named branch to disclose.
    """
    if not tree:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", tree, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_READ_TIMEOUT,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _git_head(tree: str) -> tuple[str | None, str | None]:
    """Return ``(short_commit, branch)`` for a git checkout, best-effort (``(None, None)`` else).

    Reuses ``dispatch._rev_parse_head`` (the same best-effort ``git rev-parse HEAD`` the
    post-build provenance path uses) via a function-local import — ``diagnostics → providers`` is
    the only legal import direction, and the local import keeps this module's top-level import
    graph narrow. The branch is read only when a commit was found, so a non-git tree costs one
    failed ``rev-parse`` rather than two. Blocking; callers offload it with ``asyncio.to_thread``.
    """
    # Function-local import: dispatch pulls a wide provider graph; importing it here keeps the
    # diagnostics adapter's top-level imports narrow (the service.py buildhost_agent pattern).
    from kdive.providers.shared.build_host.dispatch import _rev_parse_head

    commit = _rev_parse_head(tree)
    if commit is None:
        return None, None
    return commit[:_SHORT_COMMIT_LEN], _rev_parse_branch(tree)


async def _read_git_head(tree: str, git_head: GitHeadReader) -> tuple[str | None, str | None]:
    """Run the blocking git read off the event loop, bounded so it never breaks the verdict.

    Offloaded with ``asyncio.to_thread`` (the ``SecretRefCheck`` precedent) so the event loop is
    never blocked, and bounded by ``_GIT_READ_TIMEOUT`` (under the per-check budget). On a hang or
    any failure the git fields are left unset and the USABLE verdict stands — the disclosure is a
    best-effort extra, never a way for the check to error (#845).
    """
    try:
        async with asyncio.timeout(_GIT_READ_TIMEOUT):
            return await asyncio.to_thread(git_head, tree)
    except Exception:  # noqa: BLE001 - best-effort disclosure must never change the verdict
        _log.debug("warm-tree git HEAD read failed or timed out for %r", tree, exc_info=True)
        return None, None


def warm_tree_source_probe(
    *,
    source: Callable[[], str] = _kernel_src_from_config,
    git_head: GitHeadReader = _git_head,
) -> WarmTreeSourceProbe:
    """Build the async warm-tree source probe over the injected ``source``.

    On a ``USABLE`` source the probe also discloses the resolved path and (best-effort) the git
    HEAD short-commit and branch, so an agent can see what the warm-tree lane will build from
    before building (#845). The git read is injectable and offloaded/bounded so tests need no
    real checkout and a slow tree never breaks the verdict.

    Args:
        source: Resolves the ``KDIVE_KERNEL_SRC`` value (production: the config snapshot read;
            tests inject a fixed value). Called at probe time, not factory time.
        git_head: Reads ``(short_commit, branch)`` for a usable tree (production: ``_git_head``;
            tests inject a fixed reader). Best-effort — only consulted on ``USABLE``.

    Returns:
        An async, no-arg probe returning a :class:`WarmTreeSourceProbeResult`.
    """

    async def probe() -> WarmTreeSourceProbeResult:
        kernel_src = source()
        error = warm_tree_source_error(kernel_src)
        if error is None:
            commit, branch = await _read_git_head(kernel_src, git_head)
            return WarmTreeSourceProbeResult(
                outcome=WarmTreeSourceOutcome.USABLE,
                resolved_path=kernel_src,
                head_commit=commit,
                branch=branch,
            )
        if error == KERNEL_SRC_UNSET_DETAIL:
            return WarmTreeSourceProbeResult(outcome=WarmTreeSourceOutcome.UNSET)
        return WarmTreeSourceProbeResult(outcome=WarmTreeSourceOutcome.INVALID)

    return probe


def local_host_enabled_probe(pool: AsyncConnectionPool) -> Callable[[], Awaitable[bool]]:
    """Build the deferred probe for whether the seeded ``worker-local`` host is enabled (ADR-0167).

    Read at check time via the pool (not at factory assembly), so an operator who disables the
    seeded local host has the ``local_kernel_src`` check suppress its ``FAIL`` — closing the
    ADR-0163 exit-code regression. A DB error or a missing seeded row fails **open to enabled**
    (returns ``True``), so a transient blip never hides the latent local-lane failure the check
    exists to surface.

    Args:
        pool: The async pool used to read the build host row at probe time.

    Returns:
        An async, no-arg probe returning whether the seeded local build host is enabled.
    """

    async def probe() -> bool:
        try:
            async with pool.connection() as conn:
                host = await get_by_id(conn, WORKER_LOCAL_ID)
        except Exception:  # noqa: BLE001 - fail open to enabled; never hide the latent failure
            _log.warning(
                "local_kernel_src enabled probe DB read failed; assuming enabled", exc_info=True
            )
            return True
        return host is None or host.enabled

    return probe
