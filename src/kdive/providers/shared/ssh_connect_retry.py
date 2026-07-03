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
from typing import Literal

# Closed, fixed vocabulary for a non-zero ssh exit. Never free-form, so a reason can never leak a
# secret or a hostname into the failure record (#1008).
type SshFailureReason = Literal[
    "connection_refused",
    "banner_timeout",
    "unreachable",
    "auth_rejected",
    "host_key_mismatch",
    "remote_command_failed",
    "unknown",
]

# Single ordered stderr-phrase → reason table (first match wins). It is the one source of truth for
# both classification and retryability, so no phrase list is duplicated. Fatal auth/host-key
# phrases are listed first so they win over any co-occurring transient phrase (a rejected key must
# never be retried, even if the same stderr also mentions a reset connection).
_REASON_MARKERS: tuple[tuple[str, SshFailureReason], ...] = (
    ("host key verification failed", "host_key_mismatch"),
    ("permission denied", "auth_rejected"),
    ("too many authentication failures", "auth_rejected"),
    ("no such identity", "auth_rejected"),
    ("connection refused", "connection_refused"),
    ("connection reset", "banner_timeout"),
    ("connection closed", "banner_timeout"),
    ("closed by remote host", "banner_timeout"),
    ("connection timed out", "unreachable"),
    ("no route to host", "unreachable"),
    ("network is unreachable", "unreachable"),
)

# Reasons that mean the guest sshd is not accepting yet — transient during the
# provision→ready→authorize window, so retryable. Everything else fails fast.
_RETRYABLE_REASONS: frozenset[SshFailureReason] = frozenset(
    {"connection_refused", "banner_timeout", "unreachable"}
)

# The stderr tail is length-capped at the source before it is stored (and redacted downstream by
# the worker's failure-context Redactor path, ADR-0027).
_STDERR_TAIL_MAX = 512


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


def classify_ssh_failure(returncode: int, stderr: str | bytes) -> SshFailureReason:
    """Map a non-zero ssh exit to the closed :data:`SshFailureReason` vocabulary (#1008).

    A non-``255`` exit means ssh connected and the remote command itself exited non-zero, so it is
    always ``remote_command_failed`` regardless of stderr (ssh's own connect/auth phrases only
    appear on ``255``). A ``255`` exit is classified by the first matching stderr phrase — fatal
    auth/host-key phrases first — and falls back to ``unknown`` when nothing matches. Callers only
    invoke this on a failure; a ``0`` exit yields ``remote_command_failed`` and is not meaningful.
    """
    if returncode != 255:
        return "remote_command_failed"
    low = _stderr_text(stderr).lower()
    for phrase, reason in _REASON_MARKERS:
        if phrase in low:
            return reason
    return "unknown"


def is_sshd_starting(returncode: int, stderr: str | bytes) -> bool:
    """True when a non-zero ssh exit looks like the guest sshd is still starting (retryable).

    ssh reports both a refused connection and a rejected key as exit 255, so the two are told
    apart by stderr: a fatal-marker phrase (auth/host-key) is never retried, and a non-255 exit
    is the *remote command's* own failure (the connection succeeded) — also never retried. Derived
    from :func:`classify_ssh_failure` so the phrase table is the single source of truth.
    """
    return classify_ssh_failure(returncode, stderr) in _RETRYABLE_REASONS


def ssh_failure_tail(stderr: str | bytes) -> str:
    """Return the length-capped tail of ssh's stderr (redacted later by ``_failure_context``)."""
    return _stderr_text(stderr).strip()[-_STDERR_TAIL_MAX:]


def ssh_failure_details(returncode: int, stderr: str | bytes) -> dict[str, object]:
    """Leak-safe, diagnosable failure details for a non-zero ssh exit (#1008).

    ``reason`` is drawn from the closed :data:`SshFailureReason` vocabulary, so it can never leak a
    secret or a hostname. ``stderr_tail`` is length-capped here and redacted downstream by the
    worker's failure-context Redactor path (ADR-0027). ``exit_status`` is retained unchanged.
    """
    return {
        "exit_status": returncode,
        "reason": classify_ssh_failure(returncode, stderr),
        "stderr_tail": ssh_failure_tail(stderr),
    }


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
