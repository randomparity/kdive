"""Worker routing for activation-free System provider authority."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from kdive.domain.capacity.state import JobState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers import system_authority
from kdive.jobs.handlers.system_authority import (
    AuthoritySystemWorkerPorts,
    execute_authority_system_job,
)
from kdive.providers.core.resolver import ProviderBinding, ProviderResolver
from kdive.providers.system_authority.protocol import (
    AuthoritySystemAcknowledgementV1,
    AuthoritySystemMarkerV1,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemOperation,
    AuthoritySystemPreactivationAbsentV1,
    AuthoritySystemProvisionReadyV1,
    AuthoritySystemResponseV1,
    AuthoritySystemTakeoverRequestV1,
)
from kdive.security.secrets.secret_registry import SecretRegistry

_NOW = datetime(2026, 9, 6, tzinfo=UTC)
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


def _marker(
    operation: AuthoritySystemOperation = AuthoritySystemOperation.PROVISION,
) -> AuthoritySystemMarkerV1:
    return AuthoritySystemMarkerV1(
        system_id=uuid4(),
        allocation_id=uuid4(),
        resource_id=uuid4(),
        provider_kind="local-libvirt",
        resource_name="host-a",
        authority_instance="authority-a",
        profile_identity=_DIGEST_A,
        root_identity=_DIGEST_B,
        operation=operation,
        operation_identity=f"{operation.value}-a",
    )


def _job(marker: AuthoritySystemMarkerV1) -> Job:
    kind = (
        JobKind.PROVISION
        if marker.operation is AuthoritySystemOperation.PROVISION
        else JobKind.TEARDOWN
    )
    return Job(
        id=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        kind=kind,
        payload={
            "system_id": str(marker.system_id),
            "authority_system_v1": marker.model_dump(mode="json", by_alias=True),
        },
        state=JobState.RUNNING,
        attempt=1,
        max_attempts=3,
        worker_id="worker-a",
        lease_expires_at=datetime(2026, 9, 7, tzinfo=UTC),
        authorizing={"principal": "u", "agent_session": "s", "project": "proj"},
        dedup_key=f"{marker.system_id}:{kind.value}",
    )


class _Connection:
    @asynccontextmanager
    async def transaction(self):
        yield


class _Resolver:
    def __init__(self) -> None:
        self.calls = 0

    async def binding_for_system(self, _conn: object, system_id) -> ProviderBinding:
        self.calls += 1
        assert system_id
        return cast(ProviderBinding, object())


class _Sender:
    def __init__(self) -> None:
        self.acknowledgements = 0
        self.executions = 0
        self.takeover: AuthoritySystemTakeoverRequestV1 | None = None

    async def acknowledge_system_takeover(
        self, request: AuthoritySystemTakeoverRequestV1, *, deadline: float
    ) -> AuthoritySystemAcknowledgementV1:
        assert deadline > 0
        self.acknowledgements += 1
        self.takeover = request
        return AuthoritySystemAcknowledgementV1(
            authority_id=request.authority_id,
            generation=request.generation,
            attempt_id=request.attempt_id,
            journal_sequence=3,
            journal_digest=_DIGEST_A,
            quiescence_digest=_DIGEST_B,
        )

    async def execute_system_operation(
        self,
        request: AuthoritySystemMutationRequestV1,
        acknowledgement: AuthoritySystemAcknowledgementV1,
        *,
        deadline: float,
    ) -> AuthoritySystemResponseV1:
        assert deadline > 0 and acknowledgement.attempt_id == request.attempt_id
        self.executions += 1
        if self.executions == 1:
            raise CategorizedError("lost response", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
        fields = request.model_dump(mode="python", by_alias=True, exclude={"schema_"})
        if request.operation is AuthoritySystemOperation.PROVISION:
            proof = AuthoritySystemProvisionReadyV1(
                **fields,
                disposition="provision-ready",
                intent_identity=_DIGEST_A,
                domain_owned=True,
                root_storage_owned=True,
                boot_ready=True,
                bootstrap_ready=True,
                quarantine_retained=False,
                completed_at=_NOW,
            )
        else:
            proof = AuthoritySystemPreactivationAbsentV1(
                **fields,
                disposition="preactivation-absent",
                intent_identity=_DIGEST_A,
                domain_absent=True,
                root_storage_absent=True,
                baseline_absent=True,
                private_intent_absent=True,
                quarantine_retained=False,
                completed_at=_NOW,
            )
        return AuthoritySystemResponseV1(
            proof=proof,
            journal_sequence=7,
            journal_digest=_DIGEST_B,
        )


@pytest.mark.anyio
async def test_marked_provision_replays_lost_response_and_never_calls_worker_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = _marker()
    job = _job(marker)
    allocated = system_authority._Allocated(uuid4(), 1, _DIGEST_B)
    allocate = AsyncMock(return_value=allocated)
    acknowledge = AsyncMock(return_value=None)
    finalize = AsyncMock(return_value=None)
    ensure = AsyncMock(return_value="ssh-ed25519 YWFhYQ== kdive-system")
    monkeypatch.setattr(system_authority, "_allocate", allocate)
    monkeypatch.setattr(system_authority, "_acknowledge", acknowledge)
    monkeypatch.setattr(system_authority, "_finalize", finalize)
    monkeypatch.setattr(system_authority, "ensure_system_bootstrap_key", ensure)
    resolver = _Resolver()
    sender = _Sender()
    selected: list[AuthoritySystemMarkerV1] = []

    def sender_factory(_binding: ProviderBinding, value: AuthoritySystemMarkerV1):
        selected.append(value)
        return cast(Any, sender)

    response = await execute_authority_system_job(
        cast(Any, _Connection()),
        job,
        ports=AuthoritySystemWorkerPorts(
            resolver=cast(ProviderResolver, resolver),
            incarnation_credential=SecretStr("credential"),
            secret_registry=SecretRegistry(),
            sender_factory=sender_factory,
        ),
    )
    assert response.proof.disposition == "provision-ready"
    assert sender.acknowledgements == 1
    assert sender.executions == 2
    assert resolver.calls == 1
    assert selected == [marker]
    ensure.assert_awaited_once()
    allocate.assert_awaited_once()
    acknowledge.assert_awaited_once()
    finalize.assert_awaited_once()


@pytest.mark.anyio
async def test_marked_preactivation_teardown_creates_bootstrap_key_before_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = _marker(AuthoritySystemOperation.PREACTIVATION_TEARDOWN)
    job = _job(marker)
    allocated = system_authority._Allocated(uuid4(), 2, _DIGEST_B)
    events: list[str] = []

    async def ensure_key(*_args: object, **_kwargs: object) -> str:
        events.append("bootstrap")
        return "ssh-ed25519 YWFhYQ== kdive-system"

    async def allocate(*_args: object, **_kwargs: object) -> system_authority._Allocated:
        events.append("allocate")
        return allocated

    monkeypatch.setattr(system_authority, "ensure_system_bootstrap_key", ensure_key)
    monkeypatch.setattr(system_authority, "_allocate", allocate)
    monkeypatch.setattr(system_authority, "_acknowledge", AsyncMock(return_value=None))
    monkeypatch.setattr(system_authority, "_finalize", AsyncMock(return_value=None))
    sender = _Sender()

    response = await execute_authority_system_job(
        cast(Any, _Connection()),
        job,
        ports=AuthoritySystemWorkerPorts(
            resolver=cast(ProviderResolver, _Resolver()),
            incarnation_credential=SecretStr("credential"),
            secret_registry=SecretRegistry(),
            sender_factory=lambda _binding, _marker: cast(Any, sender),
        ),
    )

    assert response.proof.disposition == "preactivation-absent"
    assert events == ["bootstrap", "allocate"]
