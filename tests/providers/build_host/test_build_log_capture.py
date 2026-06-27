"""Build-log capture: CapturedStep, redaction + tail-cap, and failure-carrying (#770, ADR-0238)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError
from kdive.providers.shared.build_host import execution as ex
from kdive.security.secrets.secret_registry import SecretRegistry

_RUN = UUID("77777777-7777-7777-7777-777777777777")


class _Completed:
    """A stand-in CompletedProcess carrying a returncode and captured streams."""

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_captured_step_combines_streams() -> None:
    step = ex.CapturedStep.from_streams(0, "out-line\n", "err-line\n", registry=SecretRegistry())
    assert step.returncode == 0
    assert "out-line" in step.output
    assert "err-line" in step.output


def test_captured_step_keeps_tail_when_oversized() -> None:
    head = "H" * (ex.BUILD_LOG_TAIL_BYTES * 2)
    tail = "TAIL-MARKER"
    step = ex.CapturedStep.from_streams(2, head + tail, "", registry=SecretRegistry())
    assert len(step.output) <= ex.BUILD_LOG_TAIL_BYTES
    assert step.output.endswith(tail)
    assert "H" * (ex.BUILD_LOG_TAIL_BYTES * 2) not in step.output


def test_captured_step_redacts_registered_secret() -> None:
    registry = SecretRegistry()
    registry.register("s3kr3tvalue", scope=None)
    step = ex.CapturedStep.from_streams(2, "leaked s3kr3tvalue here", "", registry=registry)
    assert "s3kr3tvalue" not in step.output
    assert "[REDACTED]" in step.output


def test_captured_step_redacts_key_value_pattern() -> None:
    step = ex.CapturedStep.from_streams(2, "token=hunter2 failed", "", registry=SecretRegistry())
    assert "hunter2" not in step.output
    assert "[REDACTED]" in step.output


def test_real_run_make_returns_captured_step(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_sandbox_run(_sandbox, _argv, **_kwargs):
        return _Completed(2, "compile out", "ld: undefined reference")

    monkeypatch.setattr(ex, "sandbox_run", fake_sandbox_run)
    step = ex.real_run_make(Path("/ws"), registry=SecretRegistry())
    assert isinstance(step, ex.CapturedStep)
    assert step.returncode == 2
    assert "ld: undefined reference" in step.output


def test_run_make_target_redacts_registry_secret_in_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry-tracked secret echoed by a recipe is [REDACTED] in the persisted build log (#838).

    The fail-closed boundary: the build-log producer must thread the app's SecretRegistry, not a
    fresh empty one, so a resolved file-ref token or credentialed URL never survives into the
    agent-served build-log artifact (ADR-0238).
    """
    registry = SecretRegistry()
    registry.register("sup3r-s3cret-token", scope=None)

    def fake_sandbox_run(_sandbox, _argv, **_kwargs):
        leaked = "fetching https://u:sup3r-s3cret-token@host failed"  # pragma: allowlist secret
        return _Completed(2, leaked, "")

    monkeypatch.setattr(ex, "sandbox_run", fake_sandbox_run)
    step = ex.run_make_target(Path("/ws"), [], "make", registry=registry)
    assert "sup3r-s3cret-token" not in step.output
    assert "[REDACTED]" in step.output


def test_run_make_target_captures_timeout_partial_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_sandbox_run(_sandbox, _argv, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["make"], timeout=1, output="partial out", stderr="partial err"
        )

    monkeypatch.setattr(ex, "sandbox_run", fake_sandbox_run)
    with pytest.raises(CategorizedError) as excinfo:
        ex.run_make_target(Path("/ws"), [], "make", registry=SecretRegistry())
    err = excinfo.value
    assert "partial err" in str(err.details.get("build_log", ""))


def test_build_failure_carries_build_log() -> None:
    err = ex.build_failure("make exited non-zero", _RUN, build_log="ld: error here")
    assert err.details["build_log"] == "ld: error here"
    assert err.details["run_id"] == str(_RUN)


def test_build_failure_omits_build_log_when_absent() -> None:
    err = ex.build_failure("make exited non-zero", _RUN)
    assert "build_log" not in err.details
