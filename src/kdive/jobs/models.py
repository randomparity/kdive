"""Job-handler type and registry for the durable queue (ADR-0018).

A :data:`JobHandler` is the async callable a worker invokes for one claimed
:class:`~kdive.domain.operations.jobs.Job`; it runs the op and returns a ``result_ref``
(object-store key) or ``None``, or raises to fail the job. :class:`HandlerRegistry`
binds exactly one handler per :class:`~kdive.domain.operations.jobs.JobKind`; plane registrars
populate it at worker startup and the worker dispatches by ``Job.kind``.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from psycopg import AsyncConnection
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind

type _Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
type _ResultRef = Annotated[str, Field(min_length=1, max_length=2048)] | None
type _AdmittedOperation = Literal[
    "activate",
    "recover",
    "resolve-conflict",
    "release",
    "cleanup",
    "teardown",
    "deadline",
    "recovery-attempt",
]


def _utf8_bytes(value: str, maximum: int) -> str:
    if not 1 <= len(value.encode("utf-8")) <= maximum:
        raise ValueError(f"must be 1-{maximum} UTF-8 bytes")
    return value


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return value.astimezone(UTC)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _OpaqueRef(_ClosedModel):
    ref: str = Field(min_length=1, max_length=1024)

    @field_validator("ref")
    @classmethod
    def _bounded_ref(cls, value: str) -> str:
        return _utf8_bytes(value, 1024)


class _TerminalEvidence(_ClosedModel):
    schema_: Literal["external-boot-terminal-evidence-v1"] = Field(alias="schema")
    activation_id: UUID
    system_id: UUID
    outcome: Literal["active", "recovered"]
    composite_state: _Digest
    objects: list[_OpaqueRef] = Field(max_length=4096)
    observed_at: datetime

    _normalize_timestamp = field_validator("observed_at")(_utc_datetime)


class _ReleaseObject(_ClosedModel):
    object: _OpaqueRef
    absent: Literal[True]


class _ReleaseEvidence(_ClosedModel):
    schema_: Literal["external-boot-release-evidence-v1"] = Field(alias="schema")
    activation_id: UUID
    system_id: UUID
    store_identity: _OpaqueRef
    owner_key: _OpaqueRef
    reserved_bytes: int = Field(gt=0)
    enumeration_complete: Literal[True]
    objects: list[_ReleaseObject] = Field(max_length=1024)
    verified_at: datetime

    _normalize_timestamp = field_validator("verified_at")(_utc_datetime)


class _CleanupEvidence(_ClosedModel):
    schema_: Literal["external-boot-cleanup-evidence-v1"] = Field(alias="schema")
    activation_id: UUID
    system_id: UUID
    release_identity: _Digest
    mode: Literal["ordinary", "system_teardown"]
    teardown_identity: _Digest | None = None
    completed_at: datetime

    _normalize_timestamp = field_validator("completed_at")(_utc_datetime)


class _TeardownEvidence(_ClosedModel):
    schema_: Literal["external-boot-teardown-evidence-v1"] = Field(alias="schema")
    system_id: UUID
    system_state: Literal["torn_down"]
    observed_at: datetime

    _normalize_timestamp = field_validator("observed_at")(_utc_datetime)


class _ResultBase(_ClosedModel):
    schema_: Literal["external-boot-authority-result-v1"] = Field(
        default="external-boot-authority-result-v1", alias="schema"
    )


class _ActivateResult(_ResultBase):
    operation: Literal["activate"]
    result_ref: _ResultRef
    evidence: _TerminalEvidence
    activation_readiness_deadline: datetime

    _normalize_timestamp = field_validator("activation_readiness_deadline")(_utc_datetime)


class _RecoverResult(_ResultBase):
    operation: Literal["recover", "resolve-conflict"]
    result_ref: _ResultRef
    evidence: _TerminalEvidence


class _ReleaseResult(_ResultBase):
    operation: Literal["release"]
    result_ref: _ResultRef
    release_identity: _Digest
    evidence: _ReleaseEvidence


class _CleanupResult(_ResultBase):
    operation: Literal["cleanup"]
    result_ref: _ResultRef
    evidence: _CleanupEvidence


class _TeardownResult(_ResultBase):
    operation: Literal["teardown"]
    result_ref: _ResultRef
    teardown_evidence: _TeardownEvidence
    cleanup_evidence: _CleanupEvidence


class _DeadlineResult(_ResultBase):
    operation: Literal["deadline"]
    deadline: datetime

    _normalize_timestamp = field_validator("deadline")(_utc_datetime)


class _RecoveryAttemptResult(_ResultBase):
    operation: Literal["recovery-attempt"]
    attempt_id: UUID
    recovery_basis: Literal["recovery_point", "pre_recovery"]
    deadline: datetime

    _normalize_timestamp = field_validator("deadline")(_utc_datetime)


class _FailureContext(_ClosedModel):
    phase: Literal["admission", "preparation", "provider-call", "observation", "commit"] | None = (
        None
    )


class _FailureResult(_ResultBase):
    operation: Literal["fail"]
    error_category: ErrorCategory
    failure_context: _FailureContext
    terminal: bool


type ExternalBootResultPayload = Annotated[
    _ActivateResult
    | _RecoverResult
    | _ReleaseResult
    | _CleanupResult
    | _TeardownResult
    | _DeadlineResult
    | _RecoveryAttemptResult
    | _FailureResult,
    Field(discriminator="operation"),
]


class ExternalBootAuthorityMarkerV1(BaseModel):
    """Immutable admission facts persisted on an authority-bound boot job."""

    model_config = ConfigDict(extra="forbid")

    activation_id: UUID
    run_id: UUID
    system_id: UUID
    plan_identity: _Digest
    purpose: Literal["activate", "recover", "resolve-conflict", "release", "teardown"]
    provider_kind: Literal["local-libvirt", "remote-libvirt"]
    authority_instance: str = Field(min_length=1, max_length=255)
    operation: Literal[
        "activate",
        "recover",
        "resolve-conflict",
        "release",
        "cleanup",
        "teardown",
        "deadline",
        "recovery-attempt",
        "fail",
    ]
    operation_identity: str = Field(min_length=1, max_length=255)

    @field_validator("authority_instance", "operation_identity")
    @classmethod
    def _bounded_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be nonblank")
        return _utf8_bytes(value, 255)


class ExternalBootAuthorityResultV1(BaseModel):
    """Authenticated provider result and its exact database authority binding."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["external-boot-authority-result-v1"] = Field(
        default="external-boot-authority-result-v1", alias="schema"
    )
    authority_id: UUID
    generation: int = Field(gt=0)
    activation_id: UUID
    run_id: UUID
    system_id: UUID
    plan_identity: str
    purpose: Literal["activate", "recover", "resolve-conflict", "release", "teardown"]
    provider_kind: Literal["local-libvirt", "remote-libvirt"]
    authority_instance: str = Field(min_length=1, max_length=255)
    admitted_operation: _AdmittedOperation
    operation_identity: str = Field(min_length=1, max_length=255)
    operation_digest: str
    journal_sequence: int = Field(gt=0)
    journal_digest: str
    result: ExternalBootResultPayload

    @field_validator("plan_identity", "operation_digest", "journal_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if len(value) != 71 or not value.startswith("sha256:"):
            raise ValueError("must be a sha256 digest")
        try:
            int(value[7:], 16)
        except ValueError as exc:
            raise ValueError("must be a sha256 digest") from exc
        if value != value.lower():
            raise ValueError("must be a lowercase sha256 digest")
        return value

    @field_validator("authority_instance", "operation_identity")
    @classmethod
    def _bounded_identity(cls, value: str) -> str:
        if not 1 <= len(value.encode()) <= 255 or not value.strip():
            raise ValueError("must be 1-255 nonblank UTF-8 bytes")
        return value

    @model_validator(mode="after")
    def _operation_matches_purpose(self) -> ExternalBootAuthorityResultV1:
        operation = self.result.operation
        admitted_operation = self.admitted_operation
        allowed = {
            "activate": {"activate", "deadline", "fail"},
            "recover": {"recover", "deadline", "recovery-attempt", "fail"},
            "resolve-conflict": {"resolve-conflict", "deadline", "fail"},
            "release": {"release", "cleanup", "fail"},
            "teardown": {"teardown", "fail"},
        }
        if admitted_operation not in allowed[self.purpose] - {"fail"}:
            raise ValueError("admitted operation does not match authority purpose")
        if operation != "fail" and operation != admitted_operation:
            raise ValueError("result operation does not match authority purpose")
        result_ref = getattr(self.result, "result_ref", None)
        if result_ref is not None:
            _utf8_bytes(result_ref, 2048)
        self._validate_evidence_ownership()
        serialized = json.dumps(
            self.result.model_dump(mode="json", by_alias=True, exclude_none=True),
            separators=(",", ":"),
        ).encode()
        if len(serialized) > 131072:
            raise ValueError("serialized authority result exceeds 128 KiB")
        evidence = [
            value
            for name in ("evidence", "teardown_evidence", "cleanup_evidence")
            if (value := getattr(self.result, name, None)) is not None
        ]
        if sum(len(item.model_dump_json(by_alias=True).encode()) for item in evidence) > 65536:
            raise ValueError("serialized lifecycle evidence exceeds 64 KiB")
        return self

    def _validate_evidence_ownership(self) -> None:
        result = self.result
        evidence = getattr(result, "evidence", None)
        if evidence is not None and (
            evidence.activation_id != self.activation_id or evidence.system_id != self.system_id
        ):
            raise ValueError("lifecycle evidence ownership does not match authority binding")
        if isinstance(result, _ActivateResult) and result.evidence.outcome != "active":
            raise ValueError("activate evidence outcome must be active")
        if isinstance(result, _RecoverResult) and result.evidence.outcome != "recovered":
            raise ValueError("recovery evidence outcome must be recovered")
        if isinstance(result, _ReleaseResult):
            refs = [item.object.ref for item in result.evidence.objects]
            if refs != sorted(set(refs)):
                raise ValueError("release evidence object references must be sorted and unique")
        if (
            isinstance(result, _CleanupResult)
            and result.evidence.mode == "ordinary"
            and result.evidence.teardown_identity is not None
        ):
            raise ValueError("ordinary cleanup cannot carry teardown identity")
        if isinstance(result, _TeardownResult):
            cleanup = result.cleanup_evidence
            if (
                result.teardown_evidence.system_id != self.system_id
                or cleanup.activation_id != self.activation_id
                or cleanup.system_id != self.system_id
                or cleanup.mode != "system_teardown"
                or cleanup.teardown_identity is None
            ):
                raise ValueError("teardown evidence ownership or mode is invalid")


class ExternalBootAuthoritySuccessV1(ExternalBootAuthorityResultV1):
    """A non-failure lifecycle result under an acknowledged authority generation."""

    @model_validator(mode="after")
    def _not_failure(self) -> ExternalBootAuthoritySuccessV1:
        if self.result.operation == "fail":
            raise ValueError("success carrier cannot contain a fail operation")
        return self


class ExternalBootAuthorityFailureV1(ExternalBootAuthorityResultV1):
    """A categorized failure or retry under an acknowledged authority generation."""

    @model_validator(mode="after")
    def _failure(self) -> ExternalBootAuthorityFailureV1:
        if self.result.operation != "fail":
            raise ValueError("failure carrier requires a fail operation")
        return self


class ExternalBootAuthorityFailure(CategorizedError):
    """A provider failure carrying the authority-bound failure result."""

    def __init__(self, result: ExternalBootAuthorityFailureV1) -> None:
        failure = result.result
        if not isinstance(failure, _FailureResult):
            raise ValueError("failure carrier requires a fail operation")
        super().__init__(
            "external boot authority operation failed",
            category=failure.error_category,
            terminal=failure.terminal,
        )
        self.result = result


type JobHandlerResult = str | None | ExternalBootAuthorityResultV1
type JobHandler = Callable[[AsyncConnection, Job], Awaitable[JobHandlerResult]]


class DuplicateHandler(RuntimeError):
    """A second handler was registered for a kind that already has one."""


class HandlerRegistry:
    """A one-handler-per-kind registry the worker dispatches through."""

    def __init__(self) -> None:
        self._handlers: dict[JobKind, JobHandler] = {}

    def register(self, kind: JobKind, handler: JobHandler) -> None:
        """Bind ``handler`` to ``kind``.

        Raises:
            DuplicateHandler: A handler is already registered for ``kind`` — two
                registrars must not silently both claim a kind.
        """
        if kind in self._handlers:
            raise DuplicateHandler(f"a handler is already registered for {kind}")
        self._handlers[kind] = handler

    def get(self, kind: JobKind) -> JobHandler | None:
        return self._handlers.get(kind)
