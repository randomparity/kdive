"""Unit tests for the bounded loopback-SSH connect-retry (ADR-0289 live proof, #963).

The live proof of the per-System bootstrap key showed local-libvirt declares a System ``ready``
~46 ms before its guest sshd accepts connections, so an immediate ``authorize_ssh_key`` races the
sshd startup and fails with a connection-refused exit 255. These tests pin the classifier that
separates that transient startup failure from a real auth/host-key failure, and the retry loop's
success / fail-fast / deadline behavior. The real ``subprocess`` calls stay ``live_vm``-gated in
the call sites; the retry policy itself is deterministic and unit-tested here.
"""

from __future__ import annotations

import subprocess

import pytest

from kdive.providers.shared.ssh_connect_retry import (
    SshRetryPolicy,
    classify_ssh_failure,
    is_sshd_starting,
    run_ssh_with_retry,
    ssh_failure_details,
)


class _FakeClock:
    """Deterministic monotonic clock whose ``sleep`` advances time (no wall-clock wait)."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _proc(returncode: int, stderr: str | bytes = "") -> subprocess.CompletedProcess[object]:
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout="", stderr=stderr
    )


def test_is_sshd_starting_true_on_connection_refused() -> None:
    assert is_sshd_starting(255, "ssh: connect to host 127.0.0.1 port 34575: Connection refused")


def test_is_sshd_starting_true_on_connection_reset() -> None:
    assert is_sshd_starting(255, "kex_exchange_identification: Connection reset by peer")


def test_is_sshd_starting_true_on_timed_out() -> None:
    assert is_sshd_starting(255, "ssh: connect to host 127.0.0.1 port 22: Connection timed out")


def test_is_sshd_starting_false_on_permission_denied() -> None:
    # A real auth failure is 255 too, but it must never be retried — it is a key mismatch, not a
    # startup race.
    assert not is_sshd_starting(255, "root@127.0.0.1: Permission denied (publickey).")


def test_is_sshd_starting_false_on_host_key_failure() -> None:
    assert not is_sshd_starting(255, "Host key verification failed.")


def test_is_sshd_starting_false_on_remote_command_exit() -> None:
    # Non-255 exit means the ssh connection succeeded and the remote command itself failed.
    assert not is_sshd_starting(1, "grep: /root/.ssh/authorized_keys: No such file or directory")


def test_is_sshd_starting_false_on_zero() -> None:
    assert not is_sshd_starting(0, "")


def test_is_sshd_starting_coerces_bytes_stderr() -> None:
    assert is_sshd_starting(255, b"ssh: connect to host: Connection refused")


def test_retry_returns_first_success() -> None:
    clock = _FakeClock()
    calls = [_proc(0)]
    result = run_ssh_with_retry(calls.pop, sleep=clock.sleep, monotonic=clock.monotonic)
    assert result.returncode == 0
    assert clock.sleeps == []


def test_retry_succeeds_after_transient_refusals() -> None:
    clock = _FakeClock()
    outcomes = [_proc(0), _proc(255, "Connection refused"), _proc(255, "Connection refused")]
    calls: list[int] = []

    def run_once() -> subprocess.CompletedProcess[object]:
        calls.append(1)
        return outcomes.pop()

    result = run_ssh_with_retry(run_once, sleep=clock.sleep, monotonic=clock.monotonic)
    assert result.returncode == 0
    assert len(calls) == 3
    assert len(clock.sleeps) == 2  # two backoffs between the three attempts


def test_retry_fails_fast_on_auth_denied() -> None:
    clock = _FakeClock()
    calls: list[int] = []

    def run_once() -> subprocess.CompletedProcess[object]:
        calls.append(1)
        return _proc(255, "Permission denied (publickey).")

    result = run_ssh_with_retry(run_once, sleep=clock.sleep, monotonic=clock.monotonic)
    assert result.returncode == 255
    assert len(calls) == 1  # no retry on a real auth failure
    assert clock.sleeps == []


def test_retry_stops_at_deadline() -> None:
    clock = _FakeClock()
    calls: list[int] = []

    def run_once() -> subprocess.CompletedProcess[object]:
        calls.append(1)
        return _proc(255, "Connection refused")

    policy = SshRetryPolicy(deadline_s=5.0, initial_backoff_s=1.0, max_backoff_s=5.0)
    result = run_ssh_with_retry(
        run_once, policy=policy, sleep=clock.sleep, monotonic=clock.monotonic
    )
    assert result.returncode == 255  # gives up and returns the last failed attempt
    assert clock.now >= 5.0  # ran until the deadline
    assert len(calls) >= 2  # retried at least once before giving up
    assert sum(clock.sleeps) <= 5.0  # never sleeps past the deadline


# --- Failure classification (#1008): drive each stderr shape → closed reason vocabulary. ---


@pytest.mark.parametrize(
    ("returncode", "stderr", "expected"),
    [
        (255, "ssh: connect to host 127.0.0.1 port 3: Connection refused", "connection_refused"),
        (255, "kex_exchange_identification: Connection reset by peer", "banner_timeout"),
        (255, "kex_exchange_identification: Connection closed by remote host", "banner_timeout"),
        (255, "ssh: connect to host 127.0.0.1 port 22: Connection timed out", "unreachable"),
        (255, "ssh: connect to host 10.0.0.9 port 22: No route to host", "unreachable"),
        (255, "ssh: connect to host 10.0.0.9 port 22: Network is unreachable", "unreachable"),
        (255, "root@127.0.0.1: Permission denied (publickey).", "auth_rejected"),
        (255, "Received disconnect: Too many authentication failures", "auth_rejected"),
        (255, "Warning: Identity file /tmp/id not accessible: No such identity", "auth_rejected"),
        (255, "Host key verification failed.", "host_key_mismatch"),
        (255, "some ssh chatter with no known marker", "unknown"),
        (1, "grep: /root/.ssh/authorized_keys: No such file or directory", "remote_command_failed"),
        (2, "", "remote_command_failed"),
    ],
)
def test_classify_ssh_failure_maps_each_stderr_shape(
    returncode: int, stderr: str, expected: str
) -> None:
    assert classify_ssh_failure(returncode, stderr) == expected


def test_classify_ssh_failure_is_fatal_first_when_markers_collide() -> None:
    # A 255 whose stderr names both a transient reset AND a rejected key must classify as the
    # fatal auth failure — else a real key mismatch is reported as retryable banner noise.
    stderr = "Connection reset by peer\nroot@127.0.0.1: Permission denied (publickey)."
    assert classify_ssh_failure(255, stderr) == "auth_rejected"


def test_classify_ssh_failure_coerces_bytes_stderr() -> None:
    assert classify_ssh_failure(255, b"ssh: connect: Connection refused") == "connection_refused"


def test_ssh_failure_details_carries_reason_exit_status_and_tail() -> None:
    details = ssh_failure_details(255, "ssh: connect to host: Connection refused")
    assert details["exit_status"] == 255
    assert details["reason"] == "connection_refused"
    tail = details["stderr_tail"]
    assert isinstance(tail, str) and "Connection refused" in tail


def test_ssh_failure_details_caps_the_stderr_tail() -> None:
    tail = ssh_failure_details(1, "x" * 5000)["stderr_tail"]
    assert isinstance(tail, str)
    assert len(tail) == 512  # last 512 chars only


def test_ssh_failure_details_values_are_leak_safe_scalars() -> None:
    # exit_status is int, reason/tail are str — all pass the worker's _safe_detail scalar gate,
    # so they survive _failure_context into the persisted job record.
    details = ssh_failure_details(255, "Host key verification failed.")
    assert isinstance(details["exit_status"], int)
    assert isinstance(details["reason"], str)
    assert isinstance(details["stderr_tail"], str)
