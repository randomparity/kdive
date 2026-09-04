"""Typed worker-boundary contracts for external boot authority results."""

import asyncio
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import Any, cast, get_type_hints
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr, ValidationError

from kdive.domain.capacity.state import JobState
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.external_boot.ports import ExternalBootAuthorityExecutor
from kdive.jobs.models import (
    ExternalBootAuthorityFailure,
    ExternalBootAuthorityFailureContext,
    ExternalBootAuthorityFailureV1,
    ExternalBootAuthoritySuccessV1,
)
from kdive.jobs.worker import Worker
from kdive.jobs.worker_telemetry import JobSpan
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
)

_DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("reason", "next_action", "terminal"),
    [
        ("observed_identity_stale", "systems.get", True),
        ("reservation_not_ready", "jobs.wait", False),
        ("authority_superseded", "jobs.get", True),
    ],
)
def test_closed_cas_failure_combinations(reason: str, next_action: str, terminal: bool) -> None:
    context = ExternalBootAuthorityFailureContext(
        phase="commit", reason=cast(Any, reason), next_action=cast(Any, next_action)
    )
    result = _carrier(
        {
            "schema": "external-boot-authority-result-v1",
            "operation": "fail",
            "error_category": (
                "infrastructure_failure" if reason == "reservation_not_ready" else "stale_handle"
            ),
            "failure_context": context.model_dump(),
            "terminal": terminal,
        }
    )
    failure = ExternalBootAuthorityFailureV1.model_validate(result)
    assert cast(Any, failure.result).terminal is terminal


def test_closed_cas_failure_rejects_crossed_action_or_terminal() -> None:
    with pytest.raises(ValidationError, match="reason"):
        ExternalBootAuthorityFailureContext(
            phase="commit", reason="reservation_not_ready", next_action="jobs.get"
        )


def test_authority_executor_protocol_has_mutation_contract() -> None:
    assert get_type_hints(ExternalBootAuthorityExecutor.execute) == {
        "request": AuthorityMutationRequestV1,
        "return": AuthorityObservationV1,
    }


def _carrier(result: dict[str, object]) -> dict[str, object]:
    operation = result.get("operation")
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
        "admitted_operation": "activate" if operation == "fail" else operation,
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


class _RecordingCursor(AbstractAsyncContextManager["_RecordingCursor"]):
    def __init__(self) -> None:
        self.params: tuple[object, ...] | None = None

    async def __aenter__(self) -> _RecordingCursor:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, _query: str, params: tuple[object, ...]) -> None:
        self.params = params

    async def fetchone(self) -> dict[str, str]:
        return {"status": "superseded", "job_state": "running"}


class _RecordingConnection:
    def __init__(self) -> None:
        self.recording_cursor = _RecordingCursor()

    def transaction(self) -> _ConnectionContext:
        return _ConnectionContext()

    def cursor(self, **_kwargs: object) -> _RecordingCursor:
        return self.recording_cursor


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
        "operation": carrier.admitted_operation,
        "operation_identity": carrier.operation_identity,
    }


def _success(operation: str = "deadline") -> ExternalBootAuthoritySuccessV1:
    result: dict[str, object] = {
        "schema": "external-boot-authority-result-v1",
        "operation": operation,
        "deadline": "2026-08-29T00:01:00Z",
    }
    return ExternalBootAuthoritySuccessV1.model_validate(_carrier(result))


def test_queue_serializes_schema_alias_and_canonical_utc_timestamp() -> None:
    async def exercise() -> None:
        carrier = _success()
        conn = _RecordingConnection()
        assert (
            await queue.commit_external_boot_authority_result(
                cast(Any, conn),
                _job(_marker(carrier)),
                carrier,
                incarnation_credential=SecretStr("credential"),
            )
            is None
        )
        assert conn.recording_cursor.params is not None
        assert conn.recording_cursor.params[-2] == carrier.admitted_operation
        payload = cast(Any, conn.recording_cursor.params[-1]).obj
        assert payload["schema"] == "external-boot-authority-result-v1"
        assert "schema_" not in payload
        assert payload["deadline"].endswith("Z")

    asyncio.run(exercise())


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


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("activation_id", uuid4()),
        ("run_id", uuid4()),
        ("system_id", uuid4()),
        ("plan_identity", "sha256:" + "c" * 64),
        ("purpose", "recover"),
        ("provider_kind", "remote-libvirt"),
        ("authority_instance", "provider-2"),
        ("operation", "fail"),
        ("operation_identity", "activate-2"),
    ],
)
def test_worker_rejects_every_marker_carrier_binding_mismatch(
    monkeypatch, field: str, replacement: str | UUID
) -> None:
    async def exercise() -> None:
        carrier = _success()
        marker = _marker(carrier)
        marker[field] = str(replacement)
        authority = AsyncMock()
        generic = AsyncMock()
        monkeypatch.setattr(queue, "complete_external_boot", authority)
        monkeypatch.setattr(queue, "complete", generic)
        await _worker()._finalize_handler(_job(marker), _span(), _task_result(carrier))
        authority.assert_not_awaited()
        generic.assert_not_awaited()

    asyncio.run(exercise())


def test_carrier_rejects_foreign_evidence_ownership_and_wrong_outcome() -> None:
    activation_id = uuid4()
    system_id = uuid4()
    data = _carrier(
        {
            "schema": "external-boot-authority-result-v1",
            "operation": "activate",
            "result_ref": None,
            "evidence": {
                "schema": "external-boot-terminal-evidence-v1",
                "activation_id": uuid4(),
                "system_id": system_id,
                "outcome": "recovered",
                "composite_state": _DIGEST,
                "objects": [],
                "observed_at": "2026-08-29T00:00:00Z",
            },
            "activation_readiness_deadline": "2026-08-29T00:01:00Z",
        }
    )
    data.update({"activation_id": activation_id, "system_id": system_id})
    with pytest.raises(ValidationError, match="ownership|outcome"):
        ExternalBootAuthoritySuccessV1.model_validate(data)


def test_carrier_rejects_teardown_evidence_with_foreign_owner_or_mode() -> None:
    data = _teardown().model_dump(mode="json", by_alias=True)
    data["result"]["cleanup_evidence"]["mode"] = "ordinary"
    data["result"]["teardown_evidence"]["system_id"] = str(uuid4())
    with pytest.raises(ValidationError, match="ownership or mode"):
        ExternalBootAuthoritySuccessV1.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authority_instance", "é" * 128),
        ("operation_identity", "é" * 128),
    ],
)
def test_carrier_rejects_multibyte_identity_over_byte_limit(field: str, value: str) -> None:
    data = _carrier(
        {
            "schema": "external-boot-authority-result-v1",
            "operation": "deadline",
            "deadline": "2026-08-29T00:01:00Z",
        }
    )
    data[field] = value
    with pytest.raises(ValidationError, match="UTF-8 bytes"):
        ExternalBootAuthoritySuccessV1.model_validate(data)


def test_carrier_rejects_oversize_evidence_and_opaque_reference() -> None:
    activation_id = uuid4()
    system_id = uuid4()
    data = _carrier(
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
                "objects": [{"ref": "é" * 513}],
                "observed_at": "2026-08-29T00:00:00Z",
            },
            "activation_readiness_deadline": "2026-08-29T00:01:00Z",
        }
    )
    data.update({"activation_id": activation_id, "system_id": system_id})
    with pytest.raises(ValidationError, match="UTF-8 bytes"):
        ExternalBootAuthoritySuccessV1.model_validate(data)


def test_carrier_rejects_total_evidence_over_64_kib() -> None:
    activation_id = uuid4()
    system_id = uuid4()
    data = _carrier(
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
                "objects": [{"ref": f"{index:04d}-" + "x" * 995} for index in range(70)],
                "observed_at": "2026-08-29T00:00:00Z",
            },
            "activation_readiness_deadline": "2026-08-29T00:01:00Z",
        }
    )
    data.update({"activation_id": activation_id, "system_id": system_id})
    with pytest.raises(ValidationError, match="65536 bytes"):
        ExternalBootAuthoritySuccessV1.model_validate(data)


def test_carrier_rejects_evidence_list_over_cardinality_bound() -> None:
    activation_id = uuid4()
    system_id = uuid4()
    data = _carrier(
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
                "objects": [{"ref": f"x-{index:04d}"} for index in range(4097)],
                "observed_at": "2026-08-29T00:00:00Z",
            },
            "activation_readiness_deadline": "2026-08-29T00:01:00Z",
        }
    )
    data.update({"activation_id": activation_id, "system_id": system_id})
    with pytest.raises(ValidationError, match="65536 bytes"):
        ExternalBootAuthoritySuccessV1.model_validate(data)


def test_carrier_rejects_naive_timestamp() -> None:
    data = _carrier(
        {
            "schema": "external-boot-authority-result-v1",
            "operation": "deadline",
            "deadline": "2026-08-29T00:01:00",
        }
    )
    with pytest.raises(ValidationError, match="UTC offset"):
        ExternalBootAuthoritySuccessV1.model_validate(data)


def test_carrier_normalizes_offset_timestamp_to_utc_z() -> None:
    data = _carrier(
        {
            "schema": "external-boot-authority-result-v1",
            "operation": "deadline",
            "deadline": "2026-08-28T17:01:00-07:00",
        }
    )
    carrier = ExternalBootAuthoritySuccessV1.model_validate(data)
    serialized = carrier.result.model_dump(mode="json", by_alias=True)
    assert serialized["deadline"] == "2026-08-29T00:01:00Z"


def test_release_rejects_foreign_evidence_and_unsorted_objects() -> None:
    activation_id = uuid4()
    system_id = uuid4()
    data = _carrier(
        {
            "schema": "external-boot-authority-result-v1",
            "operation": "release",
            "result_ref": None,
            "release_identity": _DIGEST,
            "evidence": {
                "schema": "external-boot-release-evidence-v1",
                "activation_id": uuid4(),
                "system_id": system_id,
                "store_identity": {"ref": "store"},
                "owner_key": {"ref": "owner"},
                "reserved_bytes": 1,
                "enumeration_complete": True,
                "objects": [
                    {"object": {"ref": "z"}, "absent": True},
                    {"object": {"ref": "a"}, "absent": True},
                ],
                "verified_at": "2026-08-29T00:00:00Z",
            },
        }
    )
    data.update({"activation_id": activation_id, "system_id": system_id, "purpose": "release"})
    with pytest.raises(ValidationError, match="ownership|sorted"):
        ExternalBootAuthoritySuccessV1.model_validate(data)


def test_cleanup_rejects_foreign_owner_and_inconsistent_mode() -> None:
    activation_id = uuid4()
    system_id = uuid4()
    data = _carrier(
        {
            "schema": "external-boot-authority-result-v1",
            "operation": "cleanup",
            "result_ref": None,
            "evidence": {
                "schema": "external-boot-cleanup-evidence-v1",
                "activation_id": uuid4(),
                "system_id": system_id,
                "release_identity": _DIGEST,
                "mode": "ordinary",
                "teardown_identity": _DIGEST,
                "completed_at": "2026-08-29T00:00:00Z",
            },
        }
    )
    data.update({"activation_id": activation_id, "system_id": system_id, "purpose": "release"})
    with pytest.raises(ValidationError, match="ownership|ordinary cleanup"):
        ExternalBootAuthoritySuccessV1.model_validate(data)


def test_malformed_marker_with_typed_carrier_still_calls_no_adapter(monkeypatch) -> None:
    async def exercise() -> None:
        authority = AsyncMock()
        monkeypatch.setattr(queue, "complete_external_boot", authority)
        await _worker()._finalize_handler(
            _job({"bad": "marker"}), _span(), _task_result(_success())
        )
        authority.assert_not_awaited()

    asyncio.run(exercise())


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


@pytest.mark.parametrize("as_exception", [False, True])
def test_present_null_marker_fails_closed_on_success_and_exception(
    monkeypatch, as_exception: bool
) -> None:
    async def exercise() -> None:
        success = _success()
        failure = _failure(terminal=True)
        authority_success = AsyncMock()
        authority_failure = AsyncMock()
        generic_success = AsyncMock()
        generic_failure = AsyncMock()
        monkeypatch.setattr(queue, "complete_external_boot", authority_success)
        monkeypatch.setattr(queue, "fail_external_boot", authority_failure)
        monkeypatch.setattr(queue, "complete", generic_success)
        monkeypatch.setattr(queue, "fail", generic_failure)
        task = (
            _task_result(error=ExternalBootAuthorityFailure(failure))
            if as_exception
            else _task_result(success)
        )
        await _worker()._finalize_handler(_job(None), _span(), task)
        authority_success.assert_not_awaited()
        authority_failure.assert_not_awaited()
        generic_success.assert_not_awaited()
        generic_failure.assert_not_awaited()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("admitted_operation", "purpose"),
    [
        ("activate", "activate"),
        ("recover", "recover"),
        ("resolve-conflict", "resolve-conflict"),
        ("release", "release"),
        ("cleanup", "release"),
        ("teardown", "teardown"),
        ("deadline", "activate"),
        ("recovery-attempt", "recover"),
        ("fail", "activate"),
    ],
)
def test_every_admitted_operation_routes_failure_to_authority_adapter(
    monkeypatch, admitted_operation: str, purpose: str
) -> None:
    async def exercise() -> None:
        data = _carrier(
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "fail",
                "error_category": "boot_timeout",
                "failure_context": {"phase": "provider-call"},
                "terminal": True,
            }
        )
        data.update({"admitted_operation": admitted_operation, "purpose": purpose})
        carrier = ExternalBootAuthorityFailureV1.model_validate(data)
        authority = AsyncMock(return_value=SimpleNamespace(state=JobState.FAILED))
        generic = AsyncMock()
        monkeypatch.setattr(queue, "fail_external_boot", authority)
        monkeypatch.setattr(queue, "fail", generic)
        await _worker()._finalize_handler(
            _job(_marker(carrier)),
            _span(),
            _task_result(error=ExternalBootAuthorityFailure(carrier)),
        )
        authority.assert_awaited_once()
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
