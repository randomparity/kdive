"""Best-effort build provenance probes."""

from __future__ import annotations

import subprocess  # noqa: S404 - fixed argv, no shell, best-effort provenance read

DEFAULT_GIT_READ_TIMEOUT = 30.0


def rev_parse_head(tree: str, *, timeout: float = DEFAULT_GIT_READ_TIMEOUT) -> str | None:
    """Return ``git -C <tree> rev-parse HEAD`` output, or ``None`` on any failure."""
    if not tree:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", tree, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if proc.returncode != 0:
        return None
    commit = proc.stdout.strip()
    return commit or None
