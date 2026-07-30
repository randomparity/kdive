"""Deployment-vs-working-tree skew for the live-stack tier (ADR-0482 §3, issue #1630).

A stale stack is symptomatically identical to a defect: during #1610 a deployment 3,742 commits
behind `main` produced missing-tool and schema errors that read as product bugs, and locally a
spine was driven against a worker that predated its own fix. This module reads the build each
app process reports on its aux `/readyz` (ADR-0482 §1) and grades it against the checkout.

The grading is deliberately **not** ``commit != HEAD``. That fires on any working checkout, and
a preflight that cries wolf is ignored within a day. Five verdicts (:class:`SkewVerdict`) are
graded over git ancestry, and only ``stale_restart`` — the one with effectively no
false-positive rate — skips by default.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess  # noqa: S404 - git commands use fixed argv, no shell  # nosec B404
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from kdive.config.core_settings import HEALTH_BIND_ADDR
from kdive.health.aux_bind import PROCESS_DEFAULT_PORTS

#: An abbreviated-or-full git object name. Guards `_resolve` against a responder reporting a
#: ref name ("HEAD", "main"), which would otherwise resolve against *this* checkout.
_ABBREV_SHA = re.compile(r"[0-9a-f]{7,40}")

#: Env key selecting how hard the preflight bites. See :func:`skew_policy`.
POLICY_ENV = "KDIVE_STACK_SKEW_POLICY"
_GIT_TIMEOUT = 5.0
_PROBE_TIMEOUT = 3.0
_REPO_ROOT = Path(__file__).resolve().parents[3]


class SkewVerdict(StrEnum):
    """How a deployed process's build relates to the working tree (ADR-0482 §3)."""

    FRESH = "fresh"
    STALE_RESTART = "stale_restart"
    BEHIND = "behind"
    DIVERGED = "diverged"
    UNKNOWN = "unknown"


class SkewPolicy(StrEnum):
    """How hard a non-fresh verdict bites."""

    OFF = "off"
    DEFAULT = "default"
    WARN = "warn"
    STRICT = "strict"


#: Verdicts that skip under :data:`SkewPolicy.DEFAULT` (ADR-0482 §3). ``stale_restart`` alone:
#: a source file whose mtime is later than the process start is one the process provably did
#: not load, and the remedy is a single documented command.
_DEFAULT_SKIPPING = frozenset({SkewVerdict.STALE_RESTART})

_REMEDY = "restart the app tier: scripts/live-stack/up.sh"


@dataclass(frozen=True, slots=True)
class ProcessSkew:
    """One app process's verdict, with the human-readable reason."""

    process: str
    verdict: SkewVerdict
    detail: str

    def __str__(self) -> str:
        return f"{self.process}: {self.verdict.value} — {self.detail}"


@dataclass(frozen=True, slots=True)
class RepoFacts:
    """The checkout-side facts a verdict is graded against; injected so grading is pure.

    Args:
        head: Full SHA of the working tree's ``HEAD``.
        resolve: Deployed commit (any width) -> full SHA, or ``None`` if unknown to this repo.
        is_ancestor: ``(ancestor, descendant)`` -> whether the first is an ancestor of the second.
        commits_between: ``(ancestor, descendant)`` -> commit count, or ``None`` if uncountable.
        newest_modified_source_mtime: Epoch seconds of the newest source file whose *content*
            differs from ``HEAD``, or ``None`` when the tree is clean.
    """

    head: str
    resolve: Callable[[str], str | None]
    is_ancestor: Callable[[str, str], bool]
    commits_between: Callable[[str, str], int | None]
    newest_modified_source_mtime: Callable[[], float | None]


def classify(
    process: str,
    *,
    commit: str | None,
    started_at: str | None,
    facts: RepoFacts,
) -> ProcessSkew:
    """Grade one process's reported build against the working tree (ADR-0482 §3).

    Args:
        process: ``server`` / ``worker`` / ``reconciler``.
        commit: The commit the process reported, or ``None`` when it reported none.
        started_at: The process's ISO-8601 start instant, or ``None`` when unavailable.
        facts: The checkout-side facts to grade against.
    """
    if not commit:
        return ProcessSkew(
            process,
            SkewVerdict.UNKNOWN,
            "the process reports no commit (built without git or build info)",
        )
    full = facts.resolve(commit)
    if full is None:
        return ProcessSkew(
            process,
            SkewVerdict.DIVERGED,
            f"deployed commit {commit} is not a commit in this repository; "
            f"the deployment was built from a different history ({_REMEDY})",
        )
    if full == facts.head:
        return _grade_at_head(process, started_at=started_at, facts=facts)
    if facts.is_ancestor(full, facts.head):
        count = facts.commits_between(full, facts.head)
        behind = f"{count} commits" if count is not None else "an unknown number of commits"
        return ProcessSkew(
            process,
            SkewVerdict.BEHIND,
            f"deployed commit {commit} is {behind} behind HEAD ({_REMEDY})",
        )
    return ProcessSkew(
        process,
        SkewVerdict.DIVERGED,
        f"deployed commit {commit} is not an ancestor of HEAD; the deployment is on a "
        f"different branch or HEAD has been rewritten ({_REMEDY})",
    )


def _grade_at_head(process: str, *, started_at: str | None, facts: RepoFacts) -> ProcessSkew:
    """Grade a process already at ``HEAD``: fresh unless the source outran its start."""
    started = _epoch(started_at)
    if started is None:
        return ProcessSkew(
            process,
            SkewVerdict.UNKNOWN,
            f"deployed commit matches HEAD, but its start time {started_at!r} is unparseable, "
            "so a source edit made after startup cannot be ruled out",
        )
    # Only files whose *content* differs from HEAD carry information. A bare mtime walk would
    # fire on a clean tree whose timestamps were merely rewritten — a branch round-trip, a
    # `git worktree add` (which stamps every file `now`), a stash pop, an identical reformat —
    # and since this is the one verdict that skips, that would silently delete the whole live
    # tier. The process is at HEAD, so a clean tree means it is running the code on disk no
    # matter what the timestamps say.
    newest = facts.newest_modified_source_mtime()
    if newest is not None and newest > started:
        return ProcessSkew(
            process,
            SkewVerdict.STALE_RESTART,
            f"uncommitted source under src/kdive changed {newest - started:.0f}s after this "
            f"process started, so it is not running the code on disk ({_REMEDY})",
        )
    return ProcessSkew(process, SkewVerdict.FRESH, "running HEAD, started after the last edit")


def _epoch(started_at: str | None) -> float | None:
    """Parse an ISO-8601 instant to epoch seconds; ``None`` if absent, malformed, or naive.

    A naive stamp is refused rather than assumed local: on a UTC+N host that assumption
    resolves *earlier* than reality, which biases toward a false ``stale_restart`` skip.
    """
    if not started_at:
        return None
    try:
        parsed = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


# --- the real checkout-side facts -----------------------------------------------------------


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603,S607 - fixed argv, no shell  # nosec B603 B607
            ["git", "-C", str(_REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as _exc:
        return None
    return result.stdout.strip()


def _resolve(commit: str) -> str | None:
    # Resolves any width to a full SHA, so the baked 12-char form (stamp-buildinfo.sh
    # --short=12) and live git's default width compare correctly. `^{commit}` refuses to
    # resolve a tag or tree that merely shares the prefix.
    #
    # The shape check first: without it a degraded responder reporting `{"commit": "HEAD"}`
    # (or "main") would resolve to *this* checkout's tip and grade itself clean.
    if not _ABBREV_SHA.fullmatch(commit):
        return None
    return _git("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}") or None


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    try:
        result = subprocess.run(  # noqa: S603,S607 - fixed argv, no shell  # nosec B603 B607
            ["git", "-C", str(_REPO_ROOT), "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as _exc:
        return False
    return result.returncode == 0


def _commits_between(ancestor: str, descendant: str) -> int | None:
    out = _git("rev-list", "--count", f"{ancestor}..{descendant}")
    try:
        return int(out) if out else None
    except ValueError:
        return None


def _newest_modified_source_mtime() -> float | None:
    """Epoch seconds of the newest ``src/kdive`` file differing from ``HEAD``; ``None`` if clean.

    Scoped to files git reports as *modified*, not a bare mtime walk of the tree. Timestamps
    alone are not evidence: a branch round-trip, a ``git worktree add``, a stash pop or an
    identical reformat all rewrite mtimes while leaving content byte-identical to ``HEAD``.
    Since ``stale_restart`` is the only verdict that skips, grading on those would silently
    delete the live tier (ADR-0482 §3).
    """
    changed = _git("diff", "--name-only", "-z", "HEAD", "--", "src/kdive")
    if not changed:
        return None
    mtimes = []
    for name in changed.split("\0"):
        if not name:
            continue
        try:
            # Deleted paths cannot be stat'd; a concurrent editor can also remove one between
            # git's listing and this call. Either way it contributes no timestamp.
            mtimes.append((_REPO_ROOT / name).stat().st_mtime)
        except OSError:
            continue
    return max(mtimes, default=None)


def repo_facts() -> RepoFacts | None:
    """The live checkout's facts, or ``None`` when ``HEAD`` cannot be resolved (no git)."""
    head = _git("rev-parse", "HEAD")
    if not head:
        return None
    return RepoFacts(
        head=head,
        resolve=_resolve,
        is_ancestor=_is_ancestor,
        commits_between=_commits_between,
        newest_modified_source_mtime=_newest_modified_source_mtime,
    )


# --- probing the deployed processes ---------------------------------------------------------


def readyz_urls(base_url: str, env: dict[str, str] | None = None) -> dict[str, str]:
    """Aux ``/readyz`` URL per app process, on the host serving ``base_url``.

    The aux listener is loopback/pod-local by contract (ADR-0090 §5), so this only reaches a
    stack whose processes share a host with the tests — which is exactly the live-stack tier's
    topology (``scripts/live-stack/up.sh`` runs the app tier as host processes).

    An explicit ``KDIVE_HEALTH_BIND_ADDR`` wins for every process (the ADR-0090 §5 single
    source-of-truth contract), so the per-process default map does not apply and one URL is
    probed; otherwise each process is probed on its own default port.
    """
    host = urlsplit(base_url).hostname or "127.0.0.1"
    # A bare IPv6 literal from `hostname` has its brackets stripped; re-add them or the
    # rebuilt URL is unparseable.
    authority = f"[{host}]" if ":" in host else host
    explicit = (env if env is not None else os.environ).get(HEALTH_BIND_ADDR.name, "").strip()
    if explicit:
        _, _, port_text = explicit.rpartition(":")
        return {"stack": f"http://{authority}:{port_text}/readyz"}
    return {
        process: f"http://{authority}:{port}/readyz"
        for process, port in sorted(PROCESS_DEFAULT_PORTS.items())
    }


def _fetch_version(url: str) -> dict[str, object] | None:
    """The ``version`` object from an aux ``/readyz``, or ``None`` if unreachable/absent.

    A 503 body is read too: readiness does not gate the build identifier (ADR-0482 §1), and a
    stack with a backend down is exactly when the answer matters.
    """
    try:
        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT) as response:  # noqa: S310
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except ValueError, OSError, http.client.HTTPException:
            return None
    # http.client.HTTPException is neither an OSError nor wrapped by urlopen: a non-HTTP
    # listener squatting the aux port raises BadStatusLine straight through, which would error
    # every live_stack test instead of degrading to `unknown`.
    except urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException:
        return None
    version = body.get("version") if isinstance(body, dict) else None
    return version if isinstance(version, dict) else None


def probe_stack_skew(
    base_url: str,
    *,
    facts: RepoFacts | None = None,
    fetch: Callable[[str], dict[str, object] | None] = _fetch_version,
) -> list[ProcessSkew]:
    """Grade every app process reachable behind ``base_url``'s host (ADR-0482 §3).

    Args:
        base_url: The stack URL whose host carries the aux listeners.
        facts: Checkout-side facts to grade against; the live checkout's when omitted.
        fetch: Reads one process's build from its aux ``/readyz``; the real HTTP probe when
            omitted. Injected so a test can construct the no-answer case outright — staging it
            on a port cannot, because binding an ephemeral port and closing it releases the
            port back to a range a busy host's wildcard listeners answer on (#1713).
    """
    resolved = facts if facts is not None else repo_facts()
    if resolved is None:
        return [
            ProcessSkew("checkout", SkewVerdict.UNKNOWN, "cannot resolve HEAD; not a git checkout")
        ]
    results = []
    for process, url in readyz_urls(base_url).items():
        version = fetch(url)
        if version is None:
            results.append(
                ProcessSkew(
                    process,
                    SkewVerdict.UNKNOWN,
                    f"no build reported at {url} (process down, or predates ADR-0482)",
                )
            )
            continue
        commit = version.get("commit")
        started_at = version.get("started_at")
        results.append(
            classify(
                process,
                commit=commit if isinstance(commit, str) else None,
                started_at=started_at if isinstance(started_at, str) else None,
                facts=resolved,
            )
        )
    return results


# --- policy -----------------------------------------------------------------------------


def skew_policy(env: dict[str, str] | None = None) -> SkewPolicy:
    """Resolve :data:`POLICY_ENV`; an unset or unrecognized value is the default table."""
    raw = (env if env is not None else os.environ).get(POLICY_ENV, "").strip().lower()
    try:
        return SkewPolicy(raw)
    except ValueError:
        return SkewPolicy.DEFAULT


def skipping_verdicts(policy: SkewPolicy) -> frozenset[SkewVerdict]:
    """Which verdicts skip under ``policy`` (ADR-0482 §3)."""
    if policy is SkewPolicy.STRICT:
        return frozenset(v for v in SkewVerdict if v is not SkewVerdict.FRESH)
    if policy is SkewPolicy.WARN:
        return frozenset()
    return _DEFAULT_SKIPPING


def partition(
    results: list[ProcessSkew], policy: SkewPolicy
) -> tuple[list[ProcessSkew], list[ProcessSkew]]:
    """Split ``results`` into ``(skip_worthy, warn_worthy)`` under ``policy``."""
    skipping = skipping_verdicts(policy)
    skip = [r for r in results if r.verdict in skipping]
    warn = [r for r in results if r.verdict is not SkewVerdict.FRESH and r.verdict not in skipping]
    return skip, warn
