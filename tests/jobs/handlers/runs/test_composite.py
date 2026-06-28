"""Tests for the composite build_install_boot handler (ADR-0268, #866)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from kdive.domain.capacity.state import JobState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import worker
from kdive.jobs.handlers.runs import composite
from kdive.jobs.handlers.runs.ports import RunHandlerPorts
from kdive.security.secrets.secret_registry import SecretRegistry

_RUN_ID = str(uuid4())
_BUILD_HOST_ID = str(uuid4())
_PAYLOAD = {"run_id": _RUN_ID, "cmdline": None, "build_host_id": _BUILD_HOST_ID}
_NOW = datetime(2025, 1, 1)


def _make_job() -> Job:
    return Job(
        id=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        kind=JobKind.BUILD_INSTALL_BOOT,
        payload=_PAYLOAD,
        state=JobState.RUNNING,
        max_attempts=3,
        authorizing={"principal": "user", "agent_session": None, "project": "proj"},
        dedup_key="test",
    )


def _fake_ports() -> RunHandlerPorts:
    return RunHandlerPorts(
        resolver=MagicMock(),
        secret_registry=MagicMock(),
    )


def _fake_conn() -> MagicMock:
    return MagicMock()


class _Recorder:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on

    def make(self, phase: str):
        async def _h(conn, job, **kwargs):
            self.calls.append(phase)
            if phase == self.fail_on:
                raise RuntimeError(f"{phase} boom")
            return None

        return _h


def test_runs_three_phases_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """All three phase handlers run in build → install → boot order on a clean run."""
    rec = _Recorder()
    monkeypatch.setattr(composite, "build_handler", rec.make("build"))
    monkeypatch.setattr(composite, "install_handler", rec.make("install"))
    monkeypatch.setattr(composite, "boot_handler", rec.make("boot"))

    asyncio.run(composite.composite_handler(_fake_conn(), _make_job(), ports=_fake_ports()))

    assert rec.calls == ["build", "install", "boot"]


def test_short_circuits_on_install_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """When install raises, boot is never called and CompositePhaseError names the phase."""
    rec = _Recorder(fail_on="install")
    monkeypatch.setattr(composite, "build_handler", rec.make("build"))
    monkeypatch.setattr(composite, "install_handler", rec.make("install"))
    monkeypatch.setattr(composite, "boot_handler", rec.make("boot"))

    with pytest.raises(composite.CompositePhaseError) as ei:
        asyncio.run(composite.composite_handler(_fake_conn(), _make_job(), ports=_fake_ports()))

    assert ei.value.failed_phase == "install"
    assert rec.calls == ["build", "install"]  # boot never runs


def test_failed_phase_is_in_failure_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """CompositePhaseError carries failed_phase in .details so the worker persists it.

    The worker's _failure_context reads CategorizedError.details and stores each value as
    failure_context["failure_detail_{key}"], so failed_phase ends up in the persisted job row.
    """
    rec = _Recorder(fail_on="install")
    monkeypatch.setattr(composite, "build_handler", rec.make("build"))
    monkeypatch.setattr(composite, "install_handler", rec.make("install"))
    monkeypatch.setattr(composite, "boot_handler", rec.make("boot"))

    with pytest.raises(composite.CompositePhaseError) as ei:
        asyncio.run(composite.composite_handler(_fake_conn(), _make_job(), ports=_fake_ports()))

    assert ei.value.details["failed_phase"] == "install"


def test_categorized_phase_details_survive_failure_context() -> None:
    """CompositePhaseError preserves safe structured details from the failed phase."""
    cause = CategorizedError(
        "kdump fragment symbols were dropped",
        category=ErrorCategory.BUILD_FAILURE,
        details={"dropped": "CONFIG_CRASH_DUMP", "failed_phase": "wrong"},
    )

    error = composite.CompositePhaseError("build", cause)

    assert error.category == ErrorCategory.BUILD_FAILURE
    assert error.details == {
        "dropped": "CONFIG_CRASH_DUMP",
        "failed_phase": "build",
    }
    assert worker._failure_context(error, SecretRegistry()) == {
        "failure_message": "build phase failed: kdump fragment symbols were dropped",
        "failure_detail_dropped": "CONFIG_CRASH_DUMP",
        "failure_detail_failed_phase": "build",
    }
