"""Best-effort build provenance probes."""

from __future__ import annotations

import subprocess  # noqa: S404 - fixed argv, no shell, best-effort provenance read

DEFAULT_GIT_READ_TIMEOUT = 30.0


def _git_run(tree: str, *args: str, timeout: float) -> str | None:
    """Run ``git -C <tree> <args>`` read-only; return raw stdout, or ``None`` on any failure.

    Best-effort: a non-zero exit, a missing ``git``, a non-git tree, an unowned repo (git
    ``safe.directory`` / dubious-ownership refusal when the worker runs as a different uid than
    the operator-staged tree), or a timeout all return ``None`` so a provenance probe never
    raises into the build. Unlike :func:`_git_read`, an empty (but successful) stdout is returned
    as ``""`` so a caller can distinguish "succeeded with no output" from "failed".
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
    return proc.stdout


def _git_read(tree: str, *args: str, timeout: float) -> str | None:
    """Run ``git -C <tree> <args>`` read-only; return stripped stdout, or ``None`` on any failure.

    Thin wrapper over :func:`_git_run` that strips stdout and collapses an empty result to
    ``None`` (the historical behavior for the commit/tree-sha probes, where empty output means
    "nothing to report").
    """
    out = _git_run(tree, *args, timeout=timeout)
    if out is None:
        return None
    return out.strip() or None


def rev_parse_head(tree: str, *, timeout: float = DEFAULT_GIT_READ_TIMEOUT) -> str | None:
    """Return ``git -C <tree> rev-parse HEAD`` output, or ``None`` on any failure."""
    return _git_read(tree, "rev-parse", "HEAD", timeout=timeout)


def working_tree_dirty(tree: str, *, timeout: float = DEFAULT_GIT_READ_TIMEOUT) -> bool | None:
    """Return whether the staged git ``tree`` differs from ``HEAD`` (#861, ADR-0265).

    ``True`` iff ``git -C <tree> status --porcelain`` is non-empty — counting both tracked
    modifications and untracked files (``??``), the same content the warm-tree rsync mirrors.
    ``False`` on a clean tree (git exits 0 with no output). Returns ``None`` (unknowable) when
    ``tree`` is empty, not a git work tree, or any git/OS error occurs, so the caller omits
    ``dirty`` rather than guessing — critically, a *failed* ``git status`` is ``None``, never a
    spurious ``False`` that would falsely report a dirty build as clean. Gitignored paths are not
    reported by ``git status`` (see ADR-0265): ``False`` means "no tracked changes", not
    "byte-identical to a clean ``HEAD`` checkout".
    """
    if not tree:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", tree, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as _exc:
        return None
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


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


def dirty_tracked_files(
    tree: str, *, timeout: float = DEFAULT_GIT_READ_TIMEOUT
) -> list[str] | None:
    """Return the tracked paths that differ from ``HEAD`` in the staged ``tree`` (#938, ADR-0282).

    ``git -C <tree> diff --name-only -z HEAD`` lists every tracked path whose working-tree content
    differs from ``HEAD`` (modified, added-to-index, or deleted), NUL-separated so paths with
    unusual characters need no quote parsing. Untracked files are not reported (use
    :func:`has_untracked_files`). Returns an empty list on a clean tracked state (the probe
    succeeded with no changes) and ``None`` (unknowable) when ``tree`` is empty, not a git work
    tree, has no ``HEAD``, or any git/OS error occurs — so a failed probe omits the key rather
    than reporting a spurious empty list. Captures git-tracked content only (gitignored paths are
    invisible, same as ADR-0265's ``tree_sha``).
    """
    out = _git_run(tree, "diff", "--name-only", "-z", "HEAD", timeout=timeout)
    if out is None:
        return None
    return [path for path in out.split("\0") if path]


def has_untracked_files(tree: str, *, timeout: float = DEFAULT_GIT_READ_TIMEOUT) -> bool | None:
    """Return whether the staged ``tree`` has non-ignored untracked files (#938, ADR-0282).

    ``True`` iff ``git -C <tree> ls-files --others --exclude-standard -z`` lists any path.
    ``--exclude-standard`` honours ``.gitignore`` (the ADR-0265 gitignored-blind posture), so a
    gitignored file is not "untracked". Returns ``None`` (unknowable) when ``tree`` is empty, not
    a git work tree, or any git/OS error occurs, so the caller omits ``untracked`` rather than
    guessing.
    """
    out = _git_run(tree, "ls-files", "--others", "--exclude-standard", "-z", timeout=timeout)
    if out is None:
        return None
    return any(path for path in out.split("\0") if path)
