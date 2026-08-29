"""Job-handler type and registry for the durable queue (ADR-0018).

A :data:`JobHandler` is the async callable a worker invokes for one claimed
:class:`~kdive.domain.operations.jobs.Job`; it runs the op and returns a ``result_ref``
(object-store key) or ``None``, or raises to fail the job. :class:`HandlerRegistry`
binds exactly one handler per :class:`~kdive.domain.operations.jobs.JobKind`; plane registrars
populate it at worker startup and the worker dispatches by ``Job.kind``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal
from uuid import UUID

from psycopg import AsyncConnection
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdive.domain.operations.jobs import Job, JobKind


class ExternalBootAuthorityMarkerV1(BaseModel):
    """Immutable admission facts persisted on an authority-bound boot job."""

    model_config = ConfigDict(extra="forbid")

    activation_id: UUID
    run_id: UUID
    system_id: UUID
    plan_identity: str
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
    operation_identity: str = Field(min_length=1, max_length=255)
    operation_digest: str
    journal_sequence: int = Field(gt=0)
    journal_digest: str
    result: dict[str, Any]

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

    @field_validator("result")
    @classmethod
    def _result_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("schema") != "external-boot-authority-result-v1":
            raise ValueError("result must use external-boot-authority-result-v1")
        if not isinstance(value.get("operation"), str):
            raise ValueError("result operation is required")
        return value


class ExternalBootAuthoritySuccessV1(ExternalBootAuthorityResultV1):
    """A non-failure lifecycle result under an acknowledged authority generation."""

    @model_validator(mode="after")
    def _not_failure(self) -> ExternalBootAuthoritySuccessV1:
        if self.result.get("operation") == "fail":
            raise ValueError("success carrier cannot contain a fail operation")
        return self


class ExternalBootAuthorityFailureV1(ExternalBootAuthorityResultV1):
    """A categorized failure or retry under an acknowledged authority generation."""

    @model_validator(mode="after")
    def _failure(self) -> ExternalBootAuthorityFailureV1:
        if self.result.get("operation") != "fail":
            raise ValueError("failure carrier requires a fail operation")
        return self


class ExternalBootAuthorityFailure(Exception):
    """A provider failure carrying the authority-bound failure result."""

    def __init__(self, result: ExternalBootAuthorityFailureV1) -> None:
        super().__init__("external boot authority operation failed")
        self.result = result


type JobHandlerResult = str | None | ExternalBootAuthorityResultV1
type JobHandler = Callable[[AsyncConnection, Job], Awaitable[str | None]]


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
