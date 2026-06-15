"""Non-gated unit tests for shared live-stack spine contracts (ADR-0042/0045)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

import tests.integration.live_stack.spine as spine
from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import ToolResponse
from tests.integration.live_stack.spine import (
    SpinePhaseError,
    await_system_state,
    drain_job,
    phase,
)


class _FakeClient:
    def __init__(self, responses: list[ToolResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(self, name: str, **args: object) -> ToolResponse:
        self.calls.append((name, args))
        if not self._responses:
            raise AssertionError(f"unexpected {name} call with {args}")
        return self._responses.pop(0)


def _client(responses: list[ToolResponse]) -> _FakeClient:
    return _FakeClient(responses)


def _live_client(client: _FakeClient) -> Any:
    return cast(Any, client)


def _job(status: str, *, category: ErrorCategory | None = None) -> ToolResponse:
    return ToolResponse(
        object_id="job-1",
        status=status,
        error_category=category.value if category else None,
    )


def _system(status: str, *, category: ErrorCategory | None = None) -> ToolResponse:
    return ToolResponse(
        object_id="system-1",
        status=status,
        error_category=category.value if category else None,
    )


async def _no_sleep(_seconds: float) -> None:
    return None


def test_phase_names_the_failing_phase() -> None:
    """A raised exception inside a phase becomes a SpinePhaseError naming that phase."""

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            async with phase("provision"):
                raise ValueError("libvirt exploded")
        assert excinfo.value.phase == "provision"
        assert isinstance(excinfo.value.__cause__, ValueError)

    asyncio.run(_run())


def test_phase_passes_through_spine_phase_error() -> None:
    """An inner SpinePhaseError is preserved (not re-wrapped under the outer phase name)."""

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            async with phase("outer"):
                raise SpinePhaseError("boot", "job failed", error_category="infrastructure_failure")
        assert excinfo.value.phase == "boot"

    asyncio.run(_run())


def test_drain_job_waits_until_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """The job drain helper keeps polling non-terminal jobs and returns success."""
    monkeypatch.setattr(spine.asyncio, "sleep", _no_sleep)
    client = _client([_job("running"), _job("succeeded")])

    async def _run() -> None:
        result = await drain_job(_live_client(client), "build", "job-1", deadline_s=60.0)

        assert result.status == "succeeded"
        assert [name for name, _args in client.calls] == ["jobs.wait", "jobs.wait"]
        assert client.calls[0][1] == {"job_id": "job-1", "timeout_s": 60.0}

    asyncio.run(_run())


def test_drain_job_classifies_terminal_failure() -> None:
    """Terminal job failure raises a phase-scoped error with the original category."""
    client = _client([_job("failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE)])

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            await drain_job(_live_client(client), "capture", "job-1")

        assert excinfo.value.phase == "capture"
        assert excinfo.value.reason == "job failed"
        assert excinfo.value.error_category == "infrastructure_failure"

    asyncio.run(_run())


def test_drain_job_classifies_worker_stall_without_sleeping() -> None:
    """A non-terminal job past its deadline reports a worker-stall timeout."""
    client = _client([_job("running")])

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            await drain_job(_live_client(client), "install", "job-1", deadline_s=-1.0)

        assert excinfo.value.phase == "install"
        assert excinfo.value.reason == "drain_timeout"

    asyncio.run(_run())


def test_await_system_state_polls_until_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """System-state polling returns once the target state is visible."""
    monkeypatch.setattr(spine.asyncio, "sleep", _no_sleep)
    client = _client([_system("booting"), _system("ready")])

    async def _run() -> None:
        await await_system_state(_live_client(client), "provision", "system-1", "ready")

        assert [name for name, _args in client.calls] == ["systems.get", "systems.get"]
        assert client.calls[0][1] == {"system_id": "system-1"}

    asyncio.run(_run())


def test_await_system_state_classifies_error_envelope() -> None:
    """Error envelopes from systems.get keep their category on the phase failure."""
    client = _client([_system("error", category=ErrorCategory.NOT_FOUND)])

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            await await_system_state(_live_client(client), "teardown", "system-1", "torn_down")

        assert excinfo.value.phase == "teardown"
        assert excinfo.value.reason == "system error"
        assert excinfo.value.error_category == "not_found"

    asyncio.run(_run())


def test_await_system_state_classifies_timeout_without_sleeping() -> None:
    """A system that never reaches the target reports the missing target state."""
    client = _client([_system("releasing")])

    async def _run() -> None:
        with pytest.raises(SpinePhaseError) as excinfo:
            await await_system_state(
                _live_client(client), "teardown", "system-1", "torn_down", deadline_s=-1.0
            )

        assert excinfo.value.phase == "teardown"
        assert excinfo.value.reason == "system did not reach torn_down"

    asyncio.run(_run())
