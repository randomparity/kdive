"""Best-effort build provenance probes."""

from __future__ import annotations

import subprocess  # noqa: S404 - fixed argv, no shell, best-effort provenance read

DEFAULT_GIT_READ_TIMEOUT = 30.0


def _git_read(tree: str, *args: str, timeout: float) -> str | None:
    """Run ``git -C <tree> <args>`` read-only; return stripped stdout, or ``None`` on any failure.

    Best-effort: a non-zero exit, a missing ``git``, a non-git tree, an unowned repo (git
    ``safe.directory`` / dubious-ownership refusal when the worker runs as a different uid than
    the operator-staged tree), or a timeout all return ``None`` so a provenance probe never
    raises into the build.
    """
    if not tree:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", tree, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as _exc:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def rev_parse_head(tree: str, *, timeout: float = DEFAULT_GIT_READ_TIMEOUT) -> str | None:
    """Return ``git -C <tree> rev-parse HEAD`` output, or ``None`` on any failure."""
    return _git_read(tree, "rev-parse", "HEAD", timeout=timeout)


def working_tree_dirty(tree: str, *, timeout: float = DEFAULT_GIT_READ_TIMEOUT) -> bool | None:
    """Return whether the staged git ``tree`` differs from ``HEAD`` (#861, ADR-0265).

    ``True`` iff ``git -C <tree> status --porcelain`` is non-empty — counting both tracked
    modifications and untracked files (``??``), the same content the warm-tree rsync mirrors.
    Returns ``None`` (unknowable) when ``tree`` is not a git work tree or any git/OS error
    occurs, so the caller omits ``dirty`` rather than guessing. Gitignored paths are not
    reported by ``git status`` (see ADR-0265): ``False`` means "no tracked changes", not
    "byte-identical to a clean ``HEAD`` checkout".
    """
    out = _git_read(tree, "status", "--porcelain", timeout=timeout)
    if out is None:
        # Distinguish "git said nothing" (clean) from "git failed". A clean tree exits 0 with
        # empty stdout, which ``_git_read`` collapses to ``None``; re-probe membership cheaply.
        return False if _is_git_work_tree(tree, timeout=timeout) else None
    return bool(out)


def _is_git_work_tree(tree: str, *, timeout: float) -> bool:
    return _git_read(tree, "rev-parse", "--is-inside-work-tree", timeout=timeout) == "true"


def staged_tree_sha(tree: str, *, timeout: float = DEFAULT_GIT_READ_TIMEOUT) -> str | None:
    """Return a content-deterministic tree-object SHA of the tracked working tree (#861, ADR-0265).

    Captures the tracked working-tree state read-only via ``git stash create`` (which writes
    GC-reclaimed loose objects but never touches the index, working tree, or stash ref) and
    resolves it to its ``^{tree}`` — a content identity, so two builds of identical tracked
    content yield the same SHA regardless of HEAD or wall-clock. Returns ``None`` when the tree
    is clean / has no tracked changes (``stash create`` prints nothing), is not a git tree, or
    any git/OS error occurs. Untracked files are not captured (``stash create`` ignores them);
    ``working_tree_dirty`` still flags them.
    """
    stash = _git_read(tree, "stash", "create", timeout=timeout)
    if stash is None:
        return None
    return _git_read(tree, "rev-parse", f"{stash}^{{tree}}", timeout=timeout)
