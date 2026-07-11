"""System define/provision admission handlers (ADR-0025).

`systems.provision` synchronously mints a System (state ``provisioning``) for a ``granted``
Allocation from a submitted profile, flips the Allocation ``granted -> active``, and enqueues a
``provision`` job. `systems.provision_defined` admits a `defined` System by System id after its
upload window is complete. Worker-owned ``provision``/``teardown``/``reprovision`` execution lives
in ``kdive.jobs.handlers.systems``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg_pool import AsyncConnectionPool

from kdive.components.validation import ComponentSourceCapabilities
from kdive.domain.errors import CategorizedError, suppressed_detail
from kdive.domain.labels import validate_label
from kdive.log import bind_context
from kdive.mcp.responses import ResponseData, ToolResponse
from kdive.mcp.tools._common import (
    as_uuid as _as_uuid,
)
from kdive.mcp.tools._common import (
    config_error as _config_error,
)
from kdive.mcp.tools._common import (
    invalid_uuid_error as _invalid_uuid_error,
)
from kdive.mcp.tools._common import job_envelope
from kdive.mcp.tools.lifecycle.systems.view import defined_system_envelope
from kdive.profiles.provider_policy import ProfilePolicy
from kdive.profiles.types import ProvisioningProfileInput
from kdive.security.authz.context import RequestContext
from kdive.services.idempotency.envelope import (
    record_result,
    resolve_conflict,
    resolve_replay,
    validate_idempotency_key,
)
from kdive.services.systems.admission import (
    AdmissionFailure,
    AdmissionFailureReason,
    AdmissionRecovery,
    AdmissionResult,
    CreateSystemMode,
    CreateSystemRequest,
    DefinedSystemAdmitted,
    ProvisionDefinedRequest,
    ProvisionJobAdmitted,
    SystemAdmission,
    SystemRecorder,
    admission_result_from_stored,
    stored_admission_result,
)
from kdive.services.systems.validation import RootfsValidator

_RECOVERY_ACTIONS: dict[AdmissionRecovery, list[str]] = {
    AdmissionRecovery.INSPECT_SYSTEMS_AND_ALLOCATIONS: ["systems.get", "allocations.list"],
    AdmissionRecovery.PROVISION_DEFINED_SYSTEM: ["systems.provision_defined"],
    AdmissionRecovery.RECYCLE_ALLOCATION: ["allocations.release", "allocations.request"],
    AdmissionRecovery.RETRY_PROVISION: ["systems.provision"],
}


def _validated_label(object_id: str, label: str | None) -> str | None | ToolResponse:
    """Validate the client label, or return a ``configuration_error`` envelope (ADR-0264).

    Validation lives in the handler, not the service ``AdmissionFailure`` path:
    ``AdmissionFailureReason`` is a closed enum with no ``invalid_label`` member, and a handler
    check runs before the idempotency-replay lookup and any DB work, so an invalid label mints
    no System and writes no audit row.
    """
    try:
        return validate_label(label)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(object_id, exc)


def _admission_failure_data(result: AdmissionFailure) -> ResponseData:
    data: dict[str, object] = dict(result.failure_details or {})
    if result.current_status is not None:
        data["current_status"] = result.current_status
    if result.reason is AdmissionFailureReason.SYSTEM_ALREADY_DEFINED:
        data["reason"] = "use_systems.provision_defined"
    return cast(ResponseData, data)


async def _with_idempotency(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    idempotency_key: str | None,
    kind: str,
    object_id: str,
    run: Callable[[SystemRecorder | None], Awaitable[AdmissionResult]],
) -> ToolResponse:
    """Wrap a systems admission call under replay-idempotency (ADR-0193).

    ``run`` invokes the admission with the recorder it is handed (or ``None`` when unkeyed);
    the recorder persists the successful service result inside the admission transaction. A replay
    is resolved up-front; a key collision is resolved read-after-conflict.
    """
    if idempotency_key is None:
        return _admission_response(await run(None))
    try:
        validate_idempotency_key(idempotency_key)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error("idempotency_key", exc)
    async with pool.connection() as conn:
        replay = await resolve_replay(conn, principal=ctx.principal, key=idempotency_key, kind=kind)
    if replay is not None:
        return _admission_response(admission_result_from_stored(replay))

    async def _record(conn: AsyncConnection, result: AdmissionResult) -> None:
        await record_result(
            conn,
            principal=ctx.principal,
            key=idempotency_key,
            project=_result_project(result),
            kind=kind,
            result=stored_admission_result(result),
        )

    try:
        return _admission_response(await run(_record))
    except UniqueViolation:
        async with pool.connection() as conn:
            try:
                winner = await resolve_conflict(
                    conn, principal=ctx.principal, key=idempotency_key, kind=kind
                )
            except CategorizedError as exc:
                return ToolResponse.failure_from_error("idempotency_key", exc)
        return _admission_response(admission_result_from_stored(winner))


def _result_project(result: AdmissionResult) -> str:
    """The project a success result belongs to (for the idempotency row)."""
    if isinstance(result, ProvisionJobAdmitted):
        return result.job.authorizing["project"]
    if isinstance(result, DefinedSystemAdmitted):
        return result.system.project
    raise TypeError(f"cannot record idempotency for a failure result: {type(result).__name__}")


def _admission_response(result: AdmissionResult) -> ToolResponse:
    if isinstance(result, AdmissionFailure):
        actions = _RECOVERY_ACTIONS[result.recovery] if result.recovery is not None else []
        return ToolResponse.failure(
            str(result.subject_id),
            result.category,
            detail=suppressed_detail(result.category, result.failure_message),
            suggested_next_actions=actions,
            data=_admission_failure_data(result),
        )
    if isinstance(result, ProvisionJobAdmitted):
        return job_envelope(result.job, "system_id", result.system_id)
    if isinstance(result, DefinedSystemAdmitted):
        return defined_system_envelope(result.system)
    raise TypeError(f"unknown system admission result: {type(result).__name__}")


@dataclass(frozen=True, slots=True)
class SystemProvisionHandlers:
    """Provisioning handlers with provider validation seams bound at construction."""

    profile_policy: ProfilePolicy
    component_sources: ComponentSourceCapabilities
    rootfs_validator: RootfsValidator

    def _admission(self) -> SystemAdmission:
        return SystemAdmission(self.profile_policy, self.component_sources, self.rootfs_validator)

    async def provision_system(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        allocation_id: str,
        profile: ProvisioningProfileInput,
        idempotency_key: str | None = None,
        label: str | None = None,
    ) -> ToolResponse:
        """Mint a System for a ``granted`` Allocation and enqueue its provision job."""
        uid = _as_uuid(allocation_id)
        if uid is None:
            return _invalid_uuid_error("allocation_id", allocation_id)
        cleaned = _validated_label(allocation_id, label)
        if isinstance(cleaned, ToolResponse):
            return cleaned
        return await self._keyed_create(
            pool,
            ctx,
            allocation_id,
            idempotency_key,
            "systems.provision",
            uid,
            profile,
            "provision",
            cleaned,
        )

    async def provision_defined_system(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        system_id: str,
        idempotency_key: str | None = None,
    ) -> ToolResponse:
        """Admit a ``defined`` System after its upload window is complete."""
        uid = _as_uuid(system_id)
        if uid is None:
            return _invalid_uuid_error("system_id", system_id)

        async def _provision_defined(recorder: SystemRecorder | None) -> AdmissionResult:
            with bind_context(principal=ctx.principal):
                return await self._admission().provision_defined(
                    pool, ctx, ProvisionDefinedRequest(system_id=uid, recorder=recorder)
                )

        return await _with_idempotency(
            pool, ctx, idempotency_key, "systems.provision_defined", system_id, _provision_defined
        )

    async def define_system(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        *,
        allocation_id: str,
        profile: ProvisioningProfileInput,
        idempotency_key: str | None = None,
        label: str | None = None,
    ) -> ToolResponse:
        """Create a System in ``defined`` for a ``granted`` Allocation."""
        uid = _as_uuid(allocation_id)
        if uid is None:
            return _config_error(allocation_id)
        cleaned = _validated_label(allocation_id, label)
        if isinstance(cleaned, ToolResponse):
            return cleaned
        return await self._keyed_create(
            pool,
            ctx,
            allocation_id,
            idempotency_key,
            "systems.define",
            uid,
            profile,
            "define",
            cleaned,
        )

    async def _keyed_create(
        self,
        pool: AsyncConnectionPool,
        ctx: RequestContext,
        object_id: str,
        idempotency_key: str | None,
        kind: str,
        allocation_id: UUID,
        profile: ProvisioningProfileInput,
        mode: CreateSystemMode,
        label: str | None = None,
    ) -> ToolResponse:
        async def _create_for_allocation(recorder: SystemRecorder | None) -> AdmissionResult:
            with bind_context(principal=ctx.principal):
                return await self._admission().create_for_allocation(
                    pool,
                    ctx,
                    CreateSystemRequest(
                        allocation_id=allocation_id,
                        profile=profile,
                        mode=mode,
                        recorder=recorder,
                        label=label,
                    ),
                )

        return await _with_idempotency(
            pool, ctx, idempotency_key, kind, object_id, _create_for_allocation
        )
