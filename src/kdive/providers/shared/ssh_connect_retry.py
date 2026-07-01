"""Bounded retry for loopback SSH that races guest sshd startup (ADR-0289, #963).

The live proof of the per-System bootstrap key showed local-libvirt declares a System ``ready``
on its boot marker, ~46 ms before the guest sshd binds the forwarded port. An ``authorize_ssh_key``
job fires immediately and its SSH is refused (exit 255), so the documented agent flow
(``provision`` → ``ready`` → ``authorize_ssh_key``) failed on the first try. This retries the
*connection-level* SSH failures with bounded backoff so the agent path tolerates the startup
window, while failing fast on auth/host-key errors (a real misconfiguration, not a race) and on a
remote command's own non-zero exit (the connection already succeeded).
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

# stderr phrases that mean the guest sshd is not accepting yet — transient during the
# provision→ready→authorize window, so retryable.
_STARTING_MARKERS = (
    "connection refused",
    "connection reset",
    "connection closed",
    "closed by remote host",
    "connection timed out",
    "no route to host",
    "network is unreachable",
)
# stderr phrases that mean a real, non-transient failure — never retried.
_FATAL_MARKERS = (
    "permission denied",
    "host key verification failed",
    "too many authentication failures",
    "no such identity",
)


@dataclass(frozen=True, slots=True)
class SshRetryPolicy:
    """How long to keep retrying a starting sshd, and the backoff between attempts."""

    deadline_s: float = 90.0
    initial_backoff_s: float = 1.0
    max_backoff_s: float = 5.0


# Default policy, bound once so it is a value (not a call) in the ``run_ssh_with_retry`` signature.
_DEFAULT_POLICY = SshRetryPolicy()


def _stderr_text(stderr: object) -> str:
    """Coerce ``proc.stderr`` to text (drgn-live captures bytes, authorize captures text)."""
    if isinstance(stderr, bytes):
        return stderr.decode("utf-8", "replace")
    return stderr if isinstance(stderr, str) else ""


def is_sshd_starting(returncode: int, stderr: str | bytes) -> bool:
    """True when a non-zero ssh exit looks like the guest sshd is still starting (retryable).

    ssh reports both a refused connection and a rejected key as exit 255, so the two are told
    apart by stderr: a fatal-marker phrase (auth/host-key) is never retried, and a non-255 exit
    is the *remote command's* own failure (the connection succeeded) — also never retried.
    """
    if returncode == 0:
        return False
    low = _stderr_text(stderr).lower()
    if any(marker in low for marker in _FATAL_MARKERS):
        return False
    if returncode != 255:
        return False
    return any(marker in low for marker in _STARTING_MARKERS)


def run_ssh_with_retry[T](
    run_once: Callable[[], subprocess.CompletedProcess[T]],
    *,
    policy: SshRetryPolicy = _DEFAULT_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> subprocess.CompletedProcess[T]:
    """Run ``run_once`` (a fixed ssh invocation), retrying only sshd-startup failures.

    Returns the first successful :class:`~subprocess.CompletedProcess`, or the last attempt when
    the failure is not retryable or the deadline passes — the caller inspects ``returncode`` and
    raises its own typed error. Generic in the process payload so a caller's bytes/text
    ``stderr``/``stdout`` type is preserved. ``run_once`` may raise (a launch/timeout fault); that
    propagates unretried. ``sleep``/``monotonic`` are injectable for deterministic tests.
    """
    deadline = monotonic() + policy.deadline_s
    backoff = policy.initial_backoff_s
    while True:
        proc = run_once()
        if proc.returncode == 0 or not is_sshd_starting(proc.returncode, _stderr_text(proc.stderr)):
            return proc
        remaining = deadline - monotonic()
        if remaining <= 0:
            return proc
        sleep(min(backoff, remaining))
        backoff = min(backoff * 2, policy.max_backoff_s)
