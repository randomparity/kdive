"""Typed worker-boundary contracts for external boot authority results."""

import asyncio
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from kdive.domain.capacity.state import JobState
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.models import (
    ExternalBootAuthorityFailure,
    ExternalBootAuthorityFailureV1,
    ExternalBootAuthoritySuccessV1,
)
from kdive.jobs.worker import Worker
from kdive.jobs.worker_telemetry import JobSpan

_DIGEST = "sha256:" + "a" * 64


def _carrier(result: dict[str, object]) -> dict[str, object]:
    return {
        "authority_id": uuid4(),
        "generation": 1,
        "activation_id": uuid4(),
        "run_id": uuid4(),
        "system_id": uuid4(),
        "plan_identity": _DIGEST,
        "purpose": "activate",
        "provider_kind": "local-libvirt",
        "authority_instance": "provider-1",
        "operation_identity": "activate-1",
        "operation_digest": _DIGEST,
        "journal_sequence": 1,
        "journal_digest": _DIGEST,
        "result": result,
    }


def test_success_carrier_rejects_failure_operation() -> None:
    with pytest.raises(ValidationError, match="success carrier"):
        ExternalBootAuthoritySuccessV1.model_validate(
            _carrier(
                {
                    "schema": "external-boot-authority-result-v1",
                    "operation": "fail",
                    "error_category": "boot_timeout",
                    "failure_context": {},
                    "terminal": True,
                }
            )
        )


def test_failure_carrier_rejects_success_operation() -> None:
    activation_id = uuid4()
    system_id = uuid4()
    carrier = _carrier(
        {
            "schema": "external-boot-authority-result-v1",
            "operation": "activate",
            "result_ref": None,
            "evidence": {
                "schema": "external-boot-terminal-evidence-v1",
                "activation_id": activation_id,
                "system_id": system_id,
                "outcome": "active",
                "composite_state": _DIGEST,
                "objects": [],
                "observed_at": "2026-08-29T00:00:00Z",
            },
            "activation_readiness_deadline": "2026-08-29T00:01:00Z",
        }
    )
    carrier["activation_id"] = activation_id
    carrier["system_id"] = system_id
    with pytest.raises(ValidationError, match="failure carrier"):
        ExternalBootAuthorityFailureV1.model_validate(carrier)


def test_carrier_rejects_malformed_binding_and_result_schema() -> None:
    malformed = _carrier({"schema": "wrong", "operation": "activate"})
    malformed["operation_digest"] = "not-a-digest"
    with pytest.raises(ValidationError) as raised:
        ExternalBootAuthoritySuccessV1.model_validate(malformed)
    locations = {error["loc"] for error in raised.value.errors()}
    assert ("operation_digest",) in locations
    assert ("result", "activate", "schema") in locations


class _ConnectionContext(AbstractAsyncContextManager[object]):
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


class _Pool:
    def connection(self) -> _ConnectionContext:
        return _ConnectionContext()


def _job(marker: object = ...) -> Job:
    payload: dict[str, object] = {"run_id": str(uuid4())}
    if marker is not ...:
        payload["external_boot_authority_v1"] = marker
    return Job.model_construct(
        id=uuid4(),
        kind=JobKind.BOOT,
        payload=payload,
        state=JobState.RUNNING,
        attempt=1,
        max_attempts=3,
        authorizing={"principal": "p", "agent_session": None, "project": "a"},
        dedup_key="external-test",
    )


def _marker(
    carrier: ExternalBootAuthoritySuccessV1 | ExternalBootAuthorityFailureV1,
) -> dict[str, str]:
    return {
        "activation_id": str(carrier.activation_id),
        "run_id": str(carrier.run_id),
        "system_id": str(carrier.system_id),
        "plan_identity": carrier.plan_identity,
        "purpose": carrier.purpose,
        "provider_kind": carrier.provider_kind,
        "authority_instance": carrier.authority_instance,
        "operation": carrier.result.operation,
        "operation_identity": carrier.operation_identity,
    }


def _success(operation: str = "deadline") -> ExternalBootAuthoritySuccessV1:
    result: dict[str, object] = {
        "schema": "external-boot-authority-result-v1",
        "operation": operation,
        "deadline": "2026-08-29T00:01:00Z",
    }
    return ExternalBootAuthoritySuccessV1.model_validate(_carrier(result))


def _teardown() -> ExternalBootAuthoritySuccessV1:
    activation_id = uuid4()
    system_id = uuid4()
    data = _carrier(
        {
            "schema": "external-boot-authority-result-v1",
            "operation": "teardown",
            "result_ref": None,
            "teardown_evidence": {
                "schema": "external-boot-teardown-evidence-v1",
                "system_id": system_id,
                "system_state": "torn_down",
                "observed_at": "2026-08-29T00:00:00Z",
            },
            "cleanup_evidence": {
                "schema": "external-boot-cleanup-evidence-v1",
                "activation_id": activation_id,
                "system_id": system_id,
                "release_identity": _DIGEST,
                "mode": "system_teardown",
                "teardown_identity": _DIGEST,
                "completed_at": "2026-08-29T00:00:01Z",
            },
        }
    )
    data.update({"activation_id": activation_id, "system_id": system_id, "purpose": "teardown"})
    return ExternalBootAuthoritySuccessV1.model_validate(data)


def _failure(*, terminal: bool) -> ExternalBootAuthorityFailureV1:
    return ExternalBootAuthorityFailureV1.model_validate(
        _carrier(
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "fail",
                "error_category": "boot_timeout",
                "failure_context": {"phase": "provider-call"},
                "terminal": terminal,
            }
        )
    )


def _worker() -> Worker:
    worker = object.__new__(Worker)
    worker._pool = _Pool()  # type: ignore[invalid-assignment]
    worker._incarnation_credential = SecretStr("credential")
    worker._telemetry = SimpleNamespace(record_job_failure=lambda *_: None)
    worker._secret_registry = SimpleNamespace()
    return worker


def _task_result(value: object = None, *, error: Exception | None = None) -> asyncio.Task:
    async def complete() -> object:
        if error is not None:
            raise error
        return value

    return asyncio.create_task(complete())


def _span() -> JobSpan:
    return cast(JobSpan, SimpleNamespace(set_outcome=lambda *_: None))


def test_worker_routes_success_and_stale_result_through_authority_adapter(monkeypatch) -> None:
    async def exercise() -> None:
        carrier = _success()
        complete = AsyncMock(return_value=None)
        generic = AsyncMock()
        monkeypatch.setattr(queue, "complete_external_boot", complete)
        monkeypatch.setattr(queue, "complete", generic)
        await _worker()._finalize_handler(_job(_marker(carrier)), _span(), _task_result(carrier))
        complete.assert_awaited_once()
        generic.assert_not_awaited()

    asyncio.run(exercise())


def test_worker_routes_teardown_evidence_through_authority_adapter(monkeypatch) -> None:
    async def exercise() -> None:
        carrier = _teardown()
        complete = AsyncMock(return_value=SimpleNamespace())
        generic = AsyncMock()
        monkeypatch.setattr(queue, "complete_external_boot", complete)
        monkeypatch.setattr(queue, "complete", generic)
        await _worker()._finalize_handler(_job(_marker(carrier)), _span(), _task_result(carrier))
        complete.assert_awaited_once()
        generic.assert_not_awaited()

    asyncio.run(exercise())


def test_authority_provider_exception_is_categorized() -> None:
    error = ExternalBootAuthorityFailure(_failure(terminal=True))
    assert error.category.value == "boot_timeout"
    assert error.terminal is True


@pytest.mark.parametrize("terminal", [False, True])
def test_worker_routes_retry_and_terminal_exception_through_authority_adapter(
    monkeypatch, terminal: bool
) -> None:
    async def exercise() -> None:
        carrier = _failure(terminal=terminal)
        fail = AsyncMock(return_value=SimpleNamespace(state=JobState.QUEUED))
        generic = AsyncMock()
        monkeypatch.setattr(queue, "fail_external_boot", fail)
        monkeypatch.setattr(queue, "fail", generic)
        await _worker()._finalize_handler(
            _job(_marker(carrier)),
            _span(),
            _task_result(error=ExternalBootAuthorityFailure(carrier)),
        )
        fail.assert_awaited_once()
        generic.assert_not_awaited()

    asyncio.run(exercise())


@pytest.mark.parametrize("marker", [{"bad": "marker"}, {}])
def test_worker_marked_job_without_typed_carrier_fails_closed(monkeypatch, marker) -> None:
    async def exercise() -> None:
        authority = AsyncMock()
        generic = AsyncMock()
        monkeypatch.setattr(queue, "complete_external_boot", authority)
        monkeypatch.setattr(queue, "complete", generic)
        await _worker()._finalize_handler(_job(marker), _span(), _task_result(None))
        authority.assert_not_awaited()
        generic.assert_not_awaited()

    asyncio.run(exercise())


def test_worker_preserves_ordinary_completion(monkeypatch) -> None:
    async def exercise() -> None:
        generic = AsyncMock(return_value=SimpleNamespace())
        authority = AsyncMock()
        monkeypatch.setattr(queue, "complete", generic)
        monkeypatch.setattr(queue, "complete_external_boot", authority)
        await _worker()._finalize_handler(_job(), _span(), _task_result("sha256:" + "b" * 64))
        generic.assert_awaited_once()
        authority.assert_not_awaited()

    asyncio.run(exercise())
