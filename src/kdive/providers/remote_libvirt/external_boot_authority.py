"""Remote external-boot coordination and provider execution (#2200)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal, NoReturn, Protocol, cast
from uuid import UUID, uuid5

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.external_boot_values import (
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    ExternalBootPreparationObservation,
    ExternalBootPreparationRequest,
    OpaqueProviderRef,
    RecoveryObjectBinding,
    RecoveryObjectObservation,
    RecoveryPoint,
    RunningKernelObservation,
)
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.external_boot_authority.module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleRecoveryRefV2,
)
from kdive.providers.external_boot_authority.module_protocol import (
    PREPARATION_ABANDONED,
    AuthorityOperationRequest,
    PreparedModuleVolumes,
    RemoteModuleLifecycleRequestV1,
    RemoteModuleLifecycleResponseV1,
    RemoteModulePreparationBeginResponseV1,
    RemoteModuleSystemTeardownRequestV1,
    RemoteModuleTerminalPreparationResponseV1,
    RemoteModuleVolumePreparationRequestV1,
    RemoteModuleVolumePreparationResponseV1,
    authority_reference,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityCleanupEvidenceContextV1,
    AuthorityCommitContextV1,
    AuthorityConflictResolutionRequestV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityOperation,
    AuthorityPreparationMutationRequestV1,
    AuthorityServiceError,
    AuthorityTeardownMutationRequestV1,
    ObservationCategory,
    operation_is_permitted,
)
from kdive.providers.external_boot_authority.teardown import (
    AuthoritySystemTeardownFacts,
    AuthorityTeardownReservationV1,
    ProviderRecoveryRefusal,
)
from kdive.providers.remote_libvirt.external_boot_store import (
    AdmittedRemoteModulePreparation,
    PriorPower,
    RemoteCleanupQuarantineV1,
    RemoteExternalBootMaterializationRecord,
    RemoteExternalBootRecoveryRecord,
    RemoteExternalBootStore,
    RemoteFinalizedTeardownRefsV1,
    RemoteModuleTerminalRecord,
    RemoteSystemTeardownAnchorV1,
    RemoteSystemTeardownIntentV1,
    RemoteSystemTeardownPhase,
    RemoteSystemTeardownRecordV1,
    quarantine_observation,
)
from kdive.providers.remote_libvirt.lifecycle.external_boot import (
    AcknowledgedDefinition,
)
from kdive.providers.remote_libvirt.module_preparation.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)

logger = logging.getLogger(__name__)

_OBSERVATION_NAMESPACE = UUID("9cf0fa94-f250-4e5f-a950-155e7860b692")


def _activation_binding(request: AuthorityOperationRequest) -> ExternalBootActivationBinding:
    return ExternalBootActivationBinding(
        system_id=str(request.system_id),
        run_id=str(request.run_id),
        activation_id=str(request.activation_id),
    )


@dataclass(frozen=True, slots=True)
class RemoteSystemTeardownState:
    """Exact provider-private records available for one teardown subject."""

    binding: ExternalBootActivationBinding
    plan_identity: str
    preparation: RemoteModuleVolumePreparationRequestV1 | None
    terminal: RemoteModuleTerminalRecord | None
    materialization: RemoteExternalBootMaterializationRecord | None
    recovery: RemoteExternalBootRecoveryRecord | None
    finalized: RemoteFinalizedTeardownRefsV1 | None = None


@dataclass(frozen=True, slots=True)
class RemoteExternalBootObservedState:
    """One live read of a remote activation's definition, power, and owned volumes.

    ``definition`` names which recorded side the inactive definition is: ``source``,
    ``target`` or ``other``. ``recovery_objects_absent`` is true only when every boot
    artifact and module volume the recovery record owns reads absent.
    """

    definition: str
    preserved: str
    boot: str
    active: bool
    recovery_objects_absent: bool


@dataclass(frozen=True, slots=True)
class RemoteSystemTeardownInspection:
    domain_absent: bool
    overlay_absent: bool
    baseline_absent: bool
    recovery_absent: bool


class RemoteModuleVolumePreparationHost:
    """Completion-own one fixed provider-host volume preparation operation."""

    def __init__(
        self,
        prepare: Callable[[RemoteModuleOperationV1], PreparedModuleVolumes],
        executor: RemoteModulePreparationExecutor,
    ) -> None:
        self._prepare = prepare
        self._executor = executor

    async def execute(
        self, request: RemoteModuleVolumePreparationRequestV1
    ) -> RemoteModuleVolumePreparationResponseV1:
        volumes = await self._executor.run(lambda: self._prepare(request.operation))
        response = RemoteModuleVolumePreparationResponseV1.from_prepared(volumes)
        response.validate_for(request.operation)
        return response


class RemoteModulePreparationOperation(Protocol):
    async def derive_operation(
        self,
        authority: AuthorityPreparationMutationRequestV1,
        preparation: ModuleAttemptPreparationRequestV1,
    ) -> RemoteModuleOperationV1: ...

    async def observe_system_power(
        self, admitted: AdmittedRemoteModulePreparation
    ) -> PriorPower: ...

    async def stop_system(self, admitted: AdmittedRemoteModulePreparation) -> None: ...

    async def restore_system_power(
        self, admitted: AdmittedRemoteModulePreparation, prior: PriorPower
    ) -> None: ...

    async def execute(
        self, admitted: AdmittedRemoteModulePreparation
    ) -> RemoteModuleTerminalPreparationResponseV1: ...

    async def execute_lifecycle(
        self,
        request: RemoteModuleLifecycleRequestV1,
        terminal: RemoteModuleTerminalRecord,
        restored: RemoteModuleLifecycleResponseV1 | None,
        deadline: float,
    ) -> RemoteModuleLifecycleResponseV1: ...

    async def execute_system_teardown(
        self,
        request: RemoteModuleSystemTeardownRequestV1,
        terminal: RemoteModuleTerminalRecord,
        restored: RemoteModuleLifecycleResponseV1 | None,
        deadline: float,
    ) -> RemoteModuleLifecycleResponseV1: ...


async def _wait_for_owned_child(
    task: asyncio.Task[object],
) -> tuple[asyncio.CancelledError | None, int]:
    """Await one completion-owned child while recording repeated caller cancellation."""
    caller = asyncio.current_task()
    assert caller is not None
    cancelled: asyncio.CancelledError | None = None
    consumed = 0
    completed = asyncio.Event()
    task.add_done_callback(lambda _task: completed.set())
    while not completed.is_set():
        try:
            await completed.wait()
        except asyncio.CancelledError as error:
            cancelled = cancelled or error
            caller.uncancel()
            consumed += 1
    if cancelled is None and caller.cancelling() != 0:
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError as error:
            cancelled = error
            caller.uncancel()
            consumed += 1
    return cancelled, consumed


def _restore_cancellation(
    caller: asyncio.Task[object], cancelled: asyncio.CancelledError, consumed: int
) -> None:
    message = cancelled.args[0] if cancelled.args else None
    for _ in range(consumed):
        caller.cancel(message)


class DurableRemoteModuleVolumePreparationHost:
    """Resume idempotent preparation from exact provider-private durable evidence."""

    def __init__(
        self,
        store: RemoteExternalBootStore,
        host: RemoteModulePreparationOperation,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._host = host
        self._monotonic = monotonic

    async def begin(
        self,
        authority: AuthorityPreparationMutationRequestV1,
        preparation: ModuleAttemptPreparationRequestV1,
        budget_seconds: int,
    ) -> RemoteModulePreparationBeginResponseV1:
        """Derive and persist one descriptor before returning it to the worker."""
        operation = await self._host.derive_operation(authority, preparation)
        request = RemoteModuleVolumePreparationRequestV1(authority=authority, operation=operation)
        self._store.stage(request, self._monotonic() + budget_seconds)
        admitted = self._store.reopen_request(request)
        return RemoteModulePreparationBeginResponseV1(
            preparation=preparation, operation=admitted.request.operation
        )

    def execution_started(self, request: RemoteModuleVolumePreparationRequestV1) -> bool:
        """Whether an earlier execute already reached the provider for this request.

        A prior power this request recorded counts: it is written ahead of the stop, and a
        successful abandon clears it, so a crash after the stop leaves a System that must not be
        refused. A record another request wrote does not count, or a later PREPARE would skip the
        held-System recheck. A replay of a refused operation whose holders have since cleared is
        answered ``provider_conflict``, which the host never runs and ADR-0714 counts as a repeat.
        """
        self._store.reopen_request(request)
        return (
            self._store.reopen_completion(request) is not None
            or self._store.reopen_result(request) is not None
            or self._store.prior_power_started_by(request.authority)
        )

    async def _quiesce_system(self, admitted: AdmittedRemoteModulePreparation) -> None:
        """Record the System's power, then stop it, before any other provider mutation.

        ADR-0585 starts the appliance only against a shut-off System. The record is written
        ahead of the stop so a retry that finds the domain already stopped keeps the power the
        System had before this activation. A refusal before the destroy has mutated nothing.
        """
        observed = await self._host.observe_system_power(admitted)
        self._store.record_prior_power(admitted.request.authority, observed)
        await self._host.stop_system(admitted)

    async def execute(
        self, request: RemoteModuleVolumePreparationRequestV1
    ) -> RemoteModuleTerminalPreparationResponseV1:
        admitted = self._store.reopen_request(request)
        completion = self._store.reopen_completion(request)
        if completion is not None:
            if completion.response is not None:
                completion.response.validate_for(request.operation)
                return completion.response
            raise CategorizedError(
                "remote module preparation requires recovery",
                category=ErrorCategory.CONFLICT,
                details={"completion": completion.state},
            )
        if result := self._store.reopen_result(request):
            result.validate_for(request.operation)
        try:
            await self._quiesce_system(admitted)
        except Exception as failure:
            await self._raise_abandoned(admitted, failure)
        task = asyncio.create_task(self._host.execute(admitted))
        cancelled, consumed = await _wait_for_owned_child(task)
        if cancelled is not None:
            if task.cancelled() or task.exception() is not None:
                self._store.publish_failed_completion(request)
            else:
                result = task.result()
                self._store.publish_result(request, result)
            caller = asyncio.current_task()
            assert caller is not None
            _restore_cancellation(caller, cancelled, consumed)
            raise cancelled from None
        try:
            result = task.result()
        except Exception as failure:
            await self._raise_abandoned(admitted, failure)
        except BaseException:
            self._store.publish_failed_completion(request)
            raise
        self._store.publish_result(request, result)
        return result

    async def _raise_abandoned(
        self, admitted: AdmittedRemoteModulePreparation, failure: Exception
    ) -> NoReturn:
        try:
            settled = await self._abandon(admitted, failure)
        except BaseException:
            self._store.publish_failed_completion(admitted.request)
            raise
        if settled is failure:
            raise failure
        raise settled from failure

    async def _abandon(
        self, admitted: AdmittedRemoteModulePreparation, failure: Exception
    ) -> Exception:
        """Put the System back in its recorded power state, then report the failure as abandoned.

        A restored System leaves no completion behind, so the failure does not latch: the
        authority settles its journal and the retry derives a fresh attempt. When the restore
        fails the System's power is unknown, so the latch is kept and the original failure stands.
        """
        request = admitted.request
        authority = request.authority
        binding = ExternalBootActivationBinding(
            system_id=str(authority.system_id),
            run_id=str(authority.run_id),
            activation_id=str(authority.activation_id),
        )
        try:
            prior = self._store.prior_power(binding, authority.plan_identity)
            if prior is not None:
                await self._host.restore_system_power(admitted, prior)
        except Exception as restore_failure:  # noqa: BLE001 -- any failure keeps the latch
            logger.error(
                "remote module preparation abandon could not restore System power: %s: %s",
                type(restore_failure).__qualname__,
                restore_failure if isinstance(restore_failure, CategorizedError) else "",
            )
            self._store.publish_failed_completion(request)
            return failure
        if prior is not None:
            try:
                self._store.clear_prior_power(binding, authority.plan_identity)
            except OSError as clear_failure:
                logger.error(
                    "remote module preparation abandon could not clear the prior-power record: %s",
                    type(clear_failure).__qualname__,
                )
        details = dict(failure.details) if isinstance(failure, CategorizedError) else {}
        # Nothing was stopped when no power was recorded, so a failure before that mutated nothing.
        details.setdefault(
            "completion",
            "failed-after-mutation" if prior is not None else "refused-before-mutation",
        )
        details[PREPARATION_ABANDONED] = True
        return CategorizedError(
            str(failure) or "remote module preparation failed",
            category=failure.category
            if isinstance(failure, CategorizedError)
            else ErrorCategory.CONFLICT,
            details=details,
            terminal=isinstance(failure, CategorizedError) and failure.terminal,
        )

    async def observe_lifecycle(
        self, request: RemoteModuleLifecycleRequestV1
    ) -> RemoteModuleLifecycleResponseV1 | None:
        try:
            completion = self._store.reopen_lifecycle_completion(request)
        except FileNotFoundError:
            return None
        if completion is None:
            return None
        # A failed restore is re-driven: the appliance resumes from its durable phase or refuses
        # without writing (ADR-0585). A failed reap stays latched (#3469).
        if completion.response is None and request.action != "restore":
            raise CategorizedError(
                "remote module lifecycle requires recovery",
                category=ErrorCategory.CONFLICT,
                details={"completion": completion.state},
            )
        return completion.response

    async def execute_lifecycle(
        self, request: RemoteModuleLifecycleRequestV1
    ) -> RemoteModuleLifecycleResponseV1:
        deadline = self._monotonic() + request.budget_seconds
        self._store.stage_lifecycle(request, deadline)
        admitted = self._store.reopen_lifecycle(request)
        request = admitted.request
        if completion := await self.observe_lifecycle(request):
            return completion
        binding = ExternalBootActivationBinding(
            system_id=str(request.authority.system_id),
            run_id=str(request.authority.run_id),
            activation_id=str(request.authority.activation_id),
        )
        terminal = self._store.reopen_terminal(binding, request.authority.plan_identity)
        restored = self._store.reopen_restored(binding, request.authority.plan_identity)
        if request.action == "restore" and restored is not None:
            self._store.publish_lifecycle_completion(request, restored)
            return restored
        if (
            request.action == "reap"
            and restored is None
            and request.authority.operation is not AuthorityOperation.TEARDOWN
        ):
            raise CategorizedError(
                "ordinary remote module cleanup requires restored evidence",
                category=ErrorCategory.CONFLICT,
                details={"completion": "refused-before-mutation"},
            )
        # A restore runs under this call's budget: a retry's staged deadline has usually expired.
        if request.action == "reap":
            deadline = admitted.local_deadline
        task = asyncio.create_task(
            self._host.execute_lifecycle(request, terminal, restored, deadline)
        )
        cancelled, consumed = await _wait_for_owned_child(task)
        if cancelled is not None:
            self._store.publish_lifecycle_completion(
                request, None if task.cancelled() or task.exception() is not None else task.result()
            )
            caller = asyncio.current_task()
            assert caller is not None
            _restore_cancellation(caller, cancelled, consumed)
            raise cancelled from None
        try:
            response = task.result()
        except BaseException:
            self._store.publish_lifecycle_completion(request, None)
            raise
        self._store.publish_lifecycle_completion(request, response)
        return response

    async def execute_system_teardown(
        self, request: RemoteModuleSystemTeardownRequestV1
    ) -> RemoteModuleLifecycleResponseV1:
        binding = ExternalBootActivationBinding(
            system_id=str(request.authority.system_id),
            run_id=str(request.authority.run_id),
            activation_id=str(request.authority.activation_id),
        )
        terminal = self._store.reopen_terminal(binding, request.authority.plan_identity)
        # A restore that completed before a failed release leaves the restored result in the
        # scratch volume, so the reap is checked against it rather than the installed result.
        restored = self._store.reopen_restored(binding, request.authority.plan_identity)
        task = asyncio.create_task(
            self._host.execute_system_teardown(
                request, terminal, restored, self._monotonic() + request.budget_seconds
            )
        )
        cancelled, consumed = await _wait_for_owned_child(task)
        if cancelled is not None:
            caller = asyncio.current_task()
            assert caller is not None
            _restore_cancellation(caller, cancelled, consumed)
            raise cancelled from None
        return task.result()


class RemoteExternalBootOperations(Protocol):
    """The six external-boot lifecycle operations within the coordinator contract."""

    def materialize(
        self,
        plan: ExternalBootPlan,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> ExternalBootMaterialization: ...

    def prepare(
        self,
        plan: ExternalBootPlan,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        modules: RemoteModuleTerminalRecord,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> RemoteExternalBootRecoveryRecord: ...

    def activate(
        self,
        recovery: RemoteExternalBootRecoveryRecord,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> None: ...

    def observe(
        self,
        recovery: RemoteExternalBootRecoveryRecord,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> RunningKernelObservation: ...

    def recover(
        self,
        recovery: RemoteExternalBootRecoveryRecord,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> None: ...

    def cleanup(
        self,
        recovery: RemoteExternalBootRecoveryRecord,
        authority: OpaqueProviderRef,
        deadline: float,
    ) -> None: ...


class RemoteSystemTeardownOperations(Protocol):
    """Authority-owned System destruction outside the six external-boot operations."""

    def inspect_system_teardown(
        self, state: RemoteSystemTeardownState, *, domain_validated: bool
    ) -> RemoteSystemTeardownInspection: ...

    def validate_system_teardown(self, state: RemoteSystemTeardownState) -> None: ...

    def destroy_system_domain(self, state: RemoteSystemTeardownState) -> None: ...

    def undefine_system_domain(self, state: RemoteSystemTeardownState) -> None: ...

    def remove_system_artifacts(self, state: RemoteSystemTeardownState) -> None: ...

    def unfinished_preparation_absent(self, state: RemoteSystemTeardownState) -> bool: ...

    def activation_volumes_absent(
        self, binding: ExternalBootActivationBinding, refs: RemoteFinalizedTeardownRefsV1
    ) -> bool: ...


class RemoteExternalBootConflictOperations(Protocol):
    """Source restoration from an acknowledged third definition, outside the six operations."""

    def recover_acknowledged(
        self,
        recovery: RemoteExternalBootRecoveryRecord,
        authority: OpaqueProviderRef,
        acknowledged: AcknowledgedDefinition,
        deadline: float,
    ) -> None: ...


class RemoteExternalBootObservationOperations(Protocol):
    """Read-only categorized observation outside the six external-boot operations."""

    def observe_state(
        self, recovery: RemoteExternalBootRecoveryRecord, deadline: float
    ) -> RemoteExternalBootObservedState: ...


class RemoteCoordinatorOperations(
    RemoteExternalBootOperations,
    RemoteExternalBootConflictOperations,
    RemoteExternalBootObservationOperations,
    RemoteSystemTeardownOperations,
    Protocol,
):
    """The complete operation requirements of remote boot and System teardown coordination."""


class RemoteExternalBootCoordinator:
    """Coordinate external boot and System teardown over exact durable provider-host recovery."""

    def __init__(
        self,
        operations: RemoteCoordinatorOperations,
        store: RemoteExternalBootStore,
        deadline: Callable[[], float],
    ) -> None:
        self._operations = operations
        self._store = store
        self._deadline = deadline

    def materialize(
        self,
        plan: ExternalBootPlan,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> ExternalBootMaterialization:
        if binding.system_id != plan.ownership.system_id or binding.run_id != plan.ownership.run_id:
            raise ProviderRecoveryRefusal(
                "remote materialization binding differs from the requested plan"
            )
        materialization = self._operations.materialize(plan, binding, authority, self._deadline())
        if (
            materialization.plan_identity != plan.identity
            or materialization.ownership.system_id != plan.ownership.system_id
            or materialization.ownership.run_id != plan.ownership.run_id
        ):
            raise ProviderRecoveryRefusal("remote materialization differs from the requested plan")
        self._store.publish_materialization(plan, materialization)
        return materialization

    def execute_preparation(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootPreparationObservation:
        observed = self._store.observe_preparation(request)
        if observed.state != "absent":
            return observed
        self._store.refuse_finalized(request.binding, request.plan.identity)
        if request.phase == "materialize":
            materialization = self.materialize(request.plan, request.binding, request.authority)
            receipt = observed.model_copy(
                update={"state": "materialized", "materialization": materialization}
            )
        else:
            materialization = self._store.reopen_materialization_for_plan(
                request.plan
            ).materialization
            recovery = self.prepare(materialization, request.binding, request.authority)
            receipt = ExternalBootPreparationObservation(
                state="prepared",
                binding=request.binding,
                plan_identity=request.plan.identity,
                authority=request.authority,
                operation_identity=request.operation_identity,
                materialization=materialization,
                recovery_point=recovery,
            )
        return self._store.publish_preparation(request, receipt)

    def observe_preparation(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootPreparationObservation:
        return self._store.observe_preparation(request)

    def adopt_preparation(
        self,
        request: ExternalBootPreparationRequest,
        predecessor: ExternalBootPreparationRequest,
        predecessor_receipt_identity: str,
    ) -> ExternalBootPreparationObservation:
        receipt = self._store.observe_preparation(predecessor)
        if (
            receipt.state == "absent"
            or receipt.identity != predecessor_receipt_identity
            or request.phase != predecessor.phase
            or request.binding != predecessor.binding
            or request.plan.identity != predecessor.plan.identity
        ):
            raise ProviderRecoveryRefusal("preparation predecessor cannot be adopted")
        adopted = receipt.model_copy(
            update={
                "authority": request.authority,
                "operation_identity": request.operation_identity,
            }
        )
        return self._store.publish_preparation(request, adopted)

    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> RecoveryPoint:
        durable = self._store.reopen_materialization(materialization)
        modules = self._store.reopen_terminal(binding, materialization.plan_identity)
        recovery = self._operations.prepare(
            durable.plan, materialization, binding, modules, authority, self._deadline()
        )
        # PREPARE stopped the System before the appliance ran, so the live read above is
        # always "inactive"; the record written ahead of that stop is the power to restore.
        if (
            recorded := self._store.prior_power(binding, materialization.plan_identity)
        ) is not None:
            recovery = recovery.model_copy(update={"prior_power": recorded})
        if recovery.binding != binding or recovery.materialization != materialization:
            raise ProviderRecoveryRefusal("remote preparation returned a different activation")
        reference = self._store.publish_recovery(recovery)
        return RecoveryPoint(
            binding=recovery.binding,
            plan_identity=recovery.plan_identity,
            materialization_identity=recovery.materialization.identity,
            recovery_ref=reference,
            source_state=recovery.source_state,
            target_state=recovery.target_state,
        )

    def activate(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        self._operations.activate(
            self._store.reopen_recovery(recovery), authority, self._deadline()
        )

    def observe(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> RunningKernelObservation:
        return self._operations.observe(
            self._store.reopen_recovery(recovery), authority, self._deadline()
        )

    def recovery_point(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RecoveryPoint:
        """Reopen the exact provider-private recovery point after process restart."""
        return self._store.recovery_point(binding, plan_identity)

    def recovery_record(self, recovery: RecoveryPoint) -> RemoteExternalBootRecoveryRecord:
        return self._store.reopen_recovery(recovery)

    def record_cleanup_quarantine(self, receipt: RemoteCleanupQuarantineV1) -> None:
        self._store.record_cleanup_quarantine(receipt)

    def cleanup_quarantine(
        self,
        binding: ExternalBootActivationBinding,
        plan_identity: str,
    ) -> RemoteCleanupQuarantineV1 | None:
        return self._store.read_cleanup_quarantine(binding, plan_identity)

    def cleanup_finalized(self, binding: ExternalBootActivationBinding, plan_identity: str) -> bool:
        return self._store.cleanup_finalized(binding, plan_identity)

    def cleanup_quarantine_for_object(
        self, reference: OpaqueProviderRef
    ) -> RemoteCleanupQuarantineV1 | None:
        return self._store.cleanup_quarantine_for_object(reference)

    def adopt_cleanup_quarantine(self, receipt: RemoteCleanupQuarantineV1) -> None:
        self._store.adopt_cleanup_quarantine(receipt)

    def finalize_cleanup_quarantine(self, receipt: RemoteCleanupQuarantineV1) -> None:
        self._store.finalize_cleanup_quarantine(receipt)

    def recover(
        self,
        recovery: RecoveryPoint,
        authority: OpaqueProviderRef,
        acknowledged: AcknowledgedDefinition | None = None,
    ) -> None:
        record = self._store.reopen_recovery(recovery)
        if acknowledged is None:
            self._operations.recover(record, authority, self._deadline())
            return
        self._operations.recover_acknowledged(record, authority, acknowledged, self._deadline())

    def cleanup(self, recovery: RecoveryPoint) -> None:
        """Delete owned boot artifacts, fencing the record by its own PREPARE authority.

        The fence is a consistency check between two provider-private records; request
        authority is enforced by the service journal and cleanup-evidence gate.
        """
        record = self._store.reopen_recovery(recovery)
        authority = self._store.preparation_authority(recovery.binding, recovery.plan_identity)
        self._operations.cleanup(record, authority, self._deadline())

    def observe_state(self, recovery: RecoveryPoint) -> RemoteExternalBootObservedState:
        return self._operations.observe_state(
            self._store.reopen_recovery(recovery), self._deadline()
        )

    def modules_restored(self, binding: ExternalBootActivationBinding, plan_identity: str) -> bool:
        """Whether durable restore evidence exists for this activation's module tree."""
        return self._store.reopen_restored(binding, plan_identity) is not None

    def system_teardown_state(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteSystemTeardownState:
        if self._store.system_torn_down(binding.system_id):
            # A complete teardown proved its volumes absent; a half-collected store's records
            # are neither read nor trusted (ADR-0787, #3472).
            return RemoteSystemTeardownState(
                binding=binding,
                plan_identity=plan_identity,
                preparation=None,
                terminal=None,
                materialization=None,
                recovery=None,
            )
        finalized = self._store.finalized_teardown_refs(binding, plan_identity)
        try:
            terminal = self._store.reopen_terminal(binding, plan_identity)
        except FileNotFoundError:
            terminal = None
        # A finalized activation's marker names what was removed or is mid-removal, so the
        # records a partial removal may have left half-deleted are not read.
        live = finalized is None
        return RemoteSystemTeardownState(
            binding=binding,
            plan_identity=plan_identity,
            preparation=self._store.teardown_preparation(binding, plan_identity) if live else None,
            terminal=terminal,
            materialization=(
                self._store.teardown_materialization(binding, plan_identity) if live else None
            ),
            recovery=self._store.teardown_recovery(binding, plan_identity),
            finalized=finalized,
        )

    def begin_system_teardown(
        self, intent: RemoteSystemTeardownIntentV1
    ) -> RemoteSystemTeardownRecordV1:
        return self._store.begin_system_teardown(intent)

    def reopen_system_teardown(
        self, anchor: RemoteSystemTeardownAnchorV1 | RemoteSystemTeardownIntentV1
    ) -> RemoteSystemTeardownRecordV1 | None:
        return self._store.reopen_system_teardown(anchor)

    def checkpoint_system_teardown(
        self,
        record: RemoteSystemTeardownRecordV1,
        phase: RemoteSystemTeardownPhase,
        *,
        completed_at: datetime | None = None,
    ) -> RemoteSystemTeardownRecordV1:
        return self._store.checkpoint_system_teardown(record, phase, completed_at=completed_at)

    def collect_torn_down_activation(self, record: RemoteSystemTeardownRecordV1) -> None:
        """Collect every activation of a completed teardown's System that is proven safe.

        The named activation's volumes were proven absent before ``complete``. Another activation
        is collected only when finalized or when every volume its records name reads absent;
        otherwise its records stay, since they are the only pointer to those volumes. A failure
        is logged, not raised: the System is already destroyed and the ``complete`` record keeps
        refusing late stages (ADR-0787, #3472, #3486).
        """
        if record.phase != "complete":
            return
        try:
            proven, kept_plans = self._prove_siblings(record)
        except Exception:  # noqa: BLE001 - the service's following observe collects again
            logger.exception("remote store collection after System teardown failed")
            return
        plan_identity = record.intent.plan_identity
        try:
            self._store.collect_torn_down_activation(
                record.intent.binding, plan_identity, keep_plan=plan_identity in kept_plans
            )
        except Exception:  # noqa: BLE001 - one activation's failure keeps the others going
            logger.exception("remote store collection after System teardown failed")
        for binding, plan, claims in proven:
            try:
                self._store.collect_proven_activation(
                    binding, plan, claims, keep_plan=plan in kept_plans
                )
            except Exception:  # noqa: BLE001 - one activation's failure keeps the others going
                logger.exception("remote store collection after System teardown failed")

    def _prove_siblings(
        self, record: RemoteSystemTeardownRecordV1
    ) -> tuple[
        list[tuple[ExternalBootActivationBinding, str, RemoteFinalizedTeardownRefsV1 | None]],
        set[str],
    ]:
        """Other activations proven safe to collect, and the plans a kept one still uses."""
        named = (record.intent.binding, record.intent.plan_identity)
        proven: list[
            tuple[ExternalBootActivationBinding, str, RemoteFinalizedTeardownRefsV1 | None]
        ] = []
        kept_plans: set[str] = set()
        for binding, plan_identity in self._store.system_activations(named[0].system_id):
            if (binding, plan_identity) == named:
                continue
            try:
                claims = self._store.activation_claims(binding, plan_identity)
                absent = claims is None or self._operations.activation_volumes_absent(
                    binding, claims
                )
            except Exception:  # noqa: BLE001 - an unprovable activation is kept, not collected
                logger.warning("remote store cannot prove an activation's volumes", exc_info=True)
                claims, absent = None, False
            if absent:
                proven.append((binding, plan_identity, claims))
                continue
            kept_plans.add(plan_identity)
            logger.warning(
                "remote store keeps System %s run %s activation %s: a volume it names may remain",
                binding.system_id,
                binding.run_id,
                binding.activation_id,
            )
        return proven, kept_plans

    def inspect_system_teardown(
        self, state: RemoteSystemTeardownState, *, domain_validated: bool
    ) -> RemoteSystemTeardownInspection:
        return self._operations.inspect_system_teardown(state, domain_validated=domain_validated)

    def validate_system_teardown(self, state: RemoteSystemTeardownState) -> None:
        self._operations.validate_system_teardown(state)

    def destroy_system_domain(self, state: RemoteSystemTeardownState) -> None:
        self._operations.destroy_system_domain(state)

    def undefine_system_domain(self, state: RemoteSystemTeardownState) -> None:
        self._operations.undefine_system_domain(state)

    def remove_system_artifacts(self, state: RemoteSystemTeardownState) -> None:
        self._operations.remove_system_artifacts(state)

    def unfinished_preparation_absent(self, state: RemoteSystemTeardownState) -> bool:
        return self._operations.unfinished_preparation_absent(state)


class RemoteExternalBootAuthorityAdapter:
    """``AuthorityMutationAdapter`` over the remote coordinator and provider-private store.

    Every commit and observation reaches provider state only through the remote coordinator,
    which reopens the exact durable recovery record for the request's activation binding and
    plan. Release cleanup reaches deletion only through ``commit_cleanup``, behind the
    service's cleanup-evidence gate.

    Categorized observation reads the definition half live: which recorded side the inactive
    definition is, power, and whether every owned boot artifact and module volume is absent.
    The module half is durable evidence, never a live appliance read: the remote module tree
    is installed into the shared System overlay at PREPARE (ADR-0585), so the installed tree
    is present under both recorded definitions until restore evidence exists. Hence:

    - every owned object absent on the source definition is ``absent``;
    - the source definition is ``source``, with or without restore evidence, which makes the
      prepared, not-yet-activated state ``source`` rather than ``mixed``;
    - the target definition is ``target`` before restore evidence and ``mixed`` after it;
    - any other definition is ``conflict``, and an unreadable record or read is
      ``unreadable``.
    """

    def __init__(
        self,
        coordinator: RemoteExternalBootCoordinator,
        executor: RemoteModulePreparationExecutor,
        remote_module_host: DurableRemoteModuleVolumePreparationHost | None = None,
        close: Callable[[], None] | None = None,
        teardown_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._coordinator = coordinator
        self._executor = executor
        self._remote_module_host = remote_module_host
        self._close = close
        self._teardown_clock = teardown_clock

    def close(self) -> None:
        self._executor.shutdown()
        if self._close is not None:
            self._close()

    @staticmethod
    def _preparation_request(
        request: AuthorityPreparationMutationRequestV1,
    ) -> ExternalBootPreparationRequest:
        if request.operation not in {AuthorityOperation.MATERIALIZE, AuthorityOperation.PREPARE}:
            raise ProviderRecoveryRefusal(
                "remote preparation request has a non-preparation operation"
            )
        phase: Literal["materialize", "prepare"] = (
            "materialize" if request.operation is AuthorityOperation.MATERIALIZE else "prepare"
        )
        return ExternalBootPreparationRequest(
            phase=phase,
            plan=request.plan,
            binding=_activation_binding(request),
            authority=authority_reference(request),
            operation_identity=request.operation_identity,
        )

    @staticmethod
    def _preparation_observation(
        receipt: ExternalBootPreparationObservation,
    ) -> AuthorityObservationV1:
        return AuthorityObservationV1(
            observation_id=uuid5(_OBSERVATION_NAMESPACE, receipt.identity),
            category="target",
            composite_state=receipt.identity,
        )

    async def observe(
        self, request: AuthorityMutationRequestV1 | AuthorityPreparationMutationRequestV1
    ) -> AuthorityObservationV1:
        if isinstance(request, AuthorityPreparationMutationRequestV1):
            receipt = await self._executor.run(
                lambda: self._coordinator.observe_preparation(self._preparation_request(request))
            )
            return self._preparation_observation(receipt)
        return await self._executor.run(lambda: self._observe(request))

    async def commit(
        self,
        request: AuthorityMutationRequestV1 | AuthorityPreparationMutationRequestV1,
        context: AuthorityCommitContextV1,
    ) -> AuthorityObservationV1:
        if isinstance(request, AuthorityPreparationMutationRequestV1):
            if (
                context.operation_identity != request.operation_identity
                or context.attempt_id != request.attempt_id
            ):
                raise ProviderRecoveryRefusal("preparation commit context differs from request")
            receipt = await self._executor.run(
                lambda: self._coordinator.execute_preparation(self._preparation_request(request))
            )
            return self._preparation_observation(receipt)
        operation = self._require_commit_point(request, context)
        if operation is AuthorityOperation.RESOLVE_CONFLICT:
            return await self._resolve_conflict(request)
        mutation: Callable[[RecoveryPoint, OpaqueProviderRef], None] | None
        if operation is AuthorityOperation.ACTIVATE:
            mutation = self._coordinator.activate
        elif operation is AuthorityOperation.RECOVER and request.purpose in {"release", "recover"}:
            mutation = self._coordinator.recover
        elif operation in _BOOKKEEPING_COMMITS:
            mutation = None
        else:
            # Deleting commit points reach the provider only through commit_cleanup, behind the
            # service's cleanup-evidence gate.
            raise ValueError(f"remote external-boot commit point {operation.value} is not admitted")
        authority = authority_reference(request)

        def apply() -> None:
            point = self._matched_point(request)
            if mutation is not None:
                mutation(point, authority)

        await self._executor.run(apply)
        return await self.observe(request)

    async def _resolve_conflict(
        self, request: AuthorityMutationRequestV1
    ) -> AuthorityObservationV1:
        """Recover from the acknowledged identity and leave the recovery objects for the release.

        The module restore already ran on the worker, as for RECOVER; nothing is deleted here, so
        the System observes `source` and its release CLEANUP reaps and deletes later (#3414).
        """
        point = await self._executor.run(lambda: self._matched_point(request))
        acknowledged = await self._acknowledged_definition(request, point)
        authority = authority_reference(request)
        await self._executor.run(lambda: self._coordinator.recover(point, authority, acknowledged))
        return await self.observe(request)

    @staticmethod
    def _require_commit_point(
        request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> AuthorityOperation:
        operation = context.commit_point
        if (
            not operation_is_permitted(request.purpose, operation)
            or operation is not request.operation
            or context.operation_identity != request.operation_identity
            or context.attempt_id != request.attempt_id
        ):
            raise ProviderRecoveryRefusal(
                "remote external-boot commit context differs from request"
            )
        return operation

    def _matched_point(self, request: AuthorityMutationRequestV1) -> RecoveryPoint:
        """Reopen this activation's exact recovery point and prove the request names it.

        Deletion must name the recovery object it destroys; no commit may name another.
        """
        point = self._coordinator.recovery_point(
            _activation_binding(request), request.plan_identity
        )
        if (
            point.source_state.definition != request.expected_source_identity
            or point.target_state.definition != request.intended_target_identity
        ):
            raise ProviderRecoveryRefusal("remote external-boot identities differ from request")
        named = request.recovery_objects
        if any(
            item.reference != point.recovery_ref.ref
            or str(item.system_id) != point.binding.system_id
            or str(item.activation_id) != point.binding.activation_id
            for item in named
        ) or (request.operation is AuthorityOperation.CLEANUP and not named):
            raise ProviderRecoveryRefusal(
                "remote external-boot recovery objects are not this activation's"
            )
        return point

    def _finalized_cleanup(self, request: AuthorityMutationRequestV1) -> bool:
        """Whether this CLEANUP's record is gone because finalization removed it."""
        if request.operation is not AuthorityOperation.CLEANUP:
            return False
        try:
            return self._coordinator.cleanup_finalized(
                _activation_binding(request), request.plan_identity
            )
        except Exception:  # noqa: BLE001 - an unreadable marker stays the unreadable class
            logger.exception("remote cleanup finalization marker is unreadable")
            return False

    def _observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        binding = _activation_binding(request)
        state: RemoteExternalBootObservedState | None = None
        restored: bool | None = None
        category: ObservationCategory = "unreadable"
        try:
            point = self._coordinator.recovery_point(binding, request.plan_identity)
            restored = self._coordinator.modules_restored(binding, request.plan_identity)
            state = self._coordinator.observe_state(point)
        except FileNotFoundError:
            if self._finalized_cleanup(request):
                category = "absent"
            else:
                logger.exception("remote external-boot provider state is unreadable")
        except Exception:  # noqa: BLE001 - a failed read is the unreadable classification
            logger.exception("remote external-boot provider state is unreadable")
        else:
            category = (
                "conflict"
                if point.source_state.definition != request.expected_source_identity
                or point.target_state.definition != request.intended_target_identity
                else _categorize(state, restored=restored)
            )
        composite = _composite_state(request, state)
        return AuthorityObservationV1(
            observation_id=uuid5(
                _OBSERVATION_NAMESPACE,
                "\0".join(
                    (
                        str(request.attempt_id),
                        str(request.generation),
                        request.operation.value,
                        category,
                        composite,
                    )
                ),
            ),
            category=category,
            composite_state=composite,
        )

    @staticmethod
    def _teardown_anchor(
        request: AuthorityTeardownMutationRequestV1,
        context: AuthorityCommitContextV1,
    ) -> RemoteSystemTeardownAnchorV1:
        if (
            request.operation is not AuthorityOperation.TEARDOWN
            or request.purpose != "teardown"
            or context.commit_point is not AuthorityOperation.TEARDOWN
            or context.operation_identity != request.operation_identity
            or context.attempt_id != request.attempt_id
        ):
            raise ProviderRecoveryRefusal("remote System teardown context differs from request")
        return RemoteSystemTeardownAnchorV1(
            authority_id=request.authority_id,
            generation=request.generation,
            binding=ExternalBootActivationBinding(
                system_id=str(request.system_id),
                run_id=str(request.run_id),
                activation_id=str(request.activation_id),
            ),
            plan_identity=request.plan_identity,
            provider_kind=request.provider_kind,
            authority_instance=request.authority_instance,
            operation_identity=request.operation_identity,
            operation_digest=request.operation_digest,
            attempt_id=request.attempt_id,
            journal_sequence=context.journal_sequence,
            journal_digest=context.journal_digest,
        )

    @staticmethod
    def _teardown_facts(
        owner: RemoteSystemTeardownAnchorV1 | RemoteSystemTeardownIntentV1,
        record: RemoteSystemTeardownRecordV1 | None,
        inspection: RemoteSystemTeardownInspection,
    ) -> AuthoritySystemTeardownFacts:
        reservation = record.intent.reservation if record is not None else None
        completed_at = record.completed_at if record is not None else None
        complete = (
            inspection.domain_absent
            and inspection.overlay_absent
            and inspection.baseline_absent
            and inspection.recovery_absent
            and completed_at is not None
            and reservation is not None
        )
        return AuthoritySystemTeardownFacts(
            intent_identity=owner.identity,
            domain_absent=inspection.domain_absent,
            overlay_absent=inspection.overlay_absent,
            baseline_absent=inspection.baseline_absent,
            recovery_absent=inspection.recovery_absent,
            quarantine_retained=not complete,
            completed_at=completed_at,
            reservation=reservation,
        )

    async def execute_system_teardown(
        self,
        request: AuthorityTeardownMutationRequestV1,
        context: AuthorityCommitContextV1,
        reservation: AuthorityTeardownReservationV1,
    ) -> AuthoritySystemTeardownFacts:
        """Persist intent, then resume exact remote domain and storage destruction."""
        anchor = self._teardown_anchor(request, context)
        retained = AuthorityTeardownReservationV1.model_validate(
            reservation.model_dump(mode="json")
        )
        intent = RemoteSystemTeardownIntentV1.model_validate(
            anchor.model_dump(exclude={"schema_"}) | {"reservation": retained}
        )
        record = await self._executor.run(lambda: self._coordinator.begin_system_teardown(intent))
        state = await self._executor.run(
            lambda: self._coordinator.system_teardown_state(intent.binding, intent.plan_identity)
        )

        async def phase(
            expected: RemoteSystemTeardownPhase,
            achieved: RemoteSystemTeardownPhase,
            action: Callable[[], None],
        ) -> None:
            nonlocal record
            if record.phase != expected:
                return

            def complete_phase() -> RemoteSystemTeardownRecordV1:
                action()
                return self._coordinator.checkpoint_system_teardown(record, achieved)

            record = await self._executor.run(complete_phase)

        await phase(
            "intent-recorded",
            "domain-validated",
            lambda: self._coordinator.validate_system_teardown(state),
        )
        await phase(
            "domain-validated",
            "domain-destroyed",
            lambda: self._coordinator.destroy_system_domain(state),
        )
        if record.phase == "domain-destroyed":
            if state.terminal is not None:
                host = self._remote_module_host
                if host is None:
                    raise ProviderRecoveryRefusal("remote module teardown host is unavailable")
                await host.execute_system_teardown(
                    RemoteModuleSystemTeardownRequestV1(authority=request, budget_seconds=300)
                )
            elif state.preparation is not None or state.finalized is not None:
                absent = await self._executor.run(
                    lambda: self._coordinator.unfinished_preparation_absent(state)
                )
                if not absent:
                    inspection = await self._executor.run(
                        lambda: self._coordinator.inspect_system_teardown(
                            state, domain_validated=record.domain_validated
                        )
                    )
                    return self._teardown_facts(record.intent, record, inspection)
            record = await self._executor.run(
                lambda: self._coordinator.checkpoint_system_teardown(record, "recovery-removed")
            )
        await phase(
            "recovery-removed",
            "domain-undefined",
            lambda: self._coordinator.undefine_system_domain(state),
        )
        await phase(
            "domain-undefined",
            "artifacts-removed",
            lambda: self._coordinator.remove_system_artifacts(state),
        )
        inspection = await self._executor.run(
            lambda: self._coordinator.inspect_system_teardown(
                state, domain_validated=record.domain_validated
            )
        )
        if (
            record.phase == "artifacts-removed"
            and inspection.domain_absent
            and inspection.overlay_absent
            and inspection.baseline_absent
            and inspection.recovery_absent
        ):
            record = await self._executor.run(
                lambda: self._coordinator.checkpoint_system_teardown(
                    record, "complete", completed_at=self._teardown_clock()
                )
            )
        await self._executor.run(lambda: self._coordinator.collect_torn_down_activation(record))
        return self._teardown_facts(record.intent, record, inspection)

    async def observe_system_teardown(
        self,
        request: AuthorityTeardownMutationRequestV1,
        context: AuthorityCommitContextV1,
    ) -> AuthoritySystemTeardownFacts:
        """Observe retained remote teardown facts without resuming any mutation."""
        anchor = self._teardown_anchor(request, context)

        def observe() -> AuthoritySystemTeardownFacts:
            record = self._coordinator.reopen_system_teardown(anchor)
            if record is not None:
                # The service recovers an interrupted teardown by observing it (#3472).
                self._coordinator.collect_torn_down_activation(record)
            state = self._coordinator.system_teardown_state(anchor.binding, anchor.plan_identity)
            inspection = self._coordinator.inspect_system_teardown(
                state, domain_validated=record.domain_validated if record is not None else False
            )
            return self._teardown_facts(
                record.intent if record is not None else anchor, record, inspection
            )

        return await self._executor.run(observe)

    async def preparation_receipt(
        self, request: AuthorityPreparationMutationRequestV1
    ) -> ExternalBootPreparationObservation:
        return await self._executor.run(
            lambda: self._coordinator.observe_preparation(self._preparation_request(request))
        )

    async def adopt_preparation(
        self,
        request: AuthorityPreparationMutationRequestV1,
        predecessor: AuthorityPreparationMutationRequestV1,
        predecessor_receipt_identity: str,
        context: AuthorityCommitContextV1,
    ) -> AuthorityObservationV1:
        """Adopt an earlier generation's identical receipt instead of repeating the mutation."""
        self._require_commit_point(cast(AuthorityMutationRequestV1, request), context)
        receipt = await self._executor.run(
            lambda: self._coordinator.adopt_preparation(
                self._preparation_request(request),
                self._preparation_request(predecessor),
                predecessor_receipt_identity,
            )
        )
        return self._preparation_observation(receipt)

    async def cleanup_subject(self, request: AuthorityMutationRequestV1) -> str:
        binding = _activation_binding(request)

        def reopen() -> RemoteExternalBootRecoveryRecord:
            point = self._coordinator.recovery_point(binding, request.plan_identity)
            return self._coordinator.recovery_record(point)

        record = await self._executor.run(reopen)
        return record.module_recovery.operation_nonce

    async def commit_cleanup(
        self,
        request: AuthorityMutationRequestV1,
        context: AuthorityCommitContextV1,
        evidence: AuthorityCleanupEvidenceContextV1,
    ) -> AuthorityObservationV1:
        if (
            evidence.operation_identity != request.operation_identity
            or evidence.attempt_id != request.attempt_id
            or context.operation_identity != request.operation_identity
            or context.attempt_id != request.attempt_id
        ):
            raise ProviderRecoveryRefusal("remote cleanup evidence differs from exact request")

        def reopen() -> tuple[RecoveryPoint, RemoteExternalBootRecoveryRecord]:
            point = self._matched_point(request)
            return point, self._coordinator.recovery_record(point)

        point, record = await self._executor.run(reopen)
        reference = RemoteModuleRecoveryRefV2.model_validate_json(evidence.recovery_reference_json)
        if (
            reference != record.module_recovery
            or evidence.operation_nonce != reference.operation_nonce
        ):
            raise ProviderRecoveryRefusal(
                "remote cleanup evidence changed before provider deletion"
            )
        authority = authority_reference(request)
        existing = await self._executor.run(
            lambda: self._coordinator.cleanup_quarantine(point.binding, point.plan_identity)
        )
        if existing is not None and existing.operation_identity != context.operation_identity:
            raise ProviderRecoveryRefusal("remote cleanup quarantine belongs to another operation")
        acknowledged = await self._acknowledged_definition(request, point)
        await self._executor.run(lambda: self._coordinator.recover(point, authority, acknowledged))
        await self._executor.run(lambda: self._coordinator.cleanup(point))
        if request.operation is AuthorityOperation.CLEANUP:
            receipt = RemoteCleanupQuarantineV1(
                binding=point.binding,
                plan_identity=point.plan_identity,
                recovery_ref=point.recovery_ref,
                operation_identity=context.operation_identity,
                attempt_id=context.attempt_id,
                journal_sequence=context.journal_sequence,
                journal_digest=context.journal_digest,
            )
            await self._executor.run(lambda: self._coordinator.record_cleanup_quarantine(receipt))
        return await self.observe(request)

    async def cleanup_quarantine_inventory(
        self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> tuple[RecoveryObjectObservation, ...]:
        """Reopen only the exact standing private cleanup receipt.

        The receipt is compared with the proof ``context``, not the request: a retried release
        that adopted an earlier generation's cleanup still names that cleanup (ADR-0787, #3434).
        """
        if request.operation is not AuthorityOperation.CLEANUP:
            return ()
        binding = _activation_binding(request)
        receipt = await self._executor.run(
            lambda: self._coordinator.cleanup_quarantine(binding, request.plan_identity)
        )
        if receipt is None or receipt.managed:
            return ()
        if (
            receipt.operation_identity != context.operation_identity
            or receipt.attempt_id != context.attempt_id
        ):
            raise AuthorityServiceError("provider_conflict")
        return (quarantine_observation(receipt),)

    async def finalize(
        self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> None:
        """Remove the cleaned recovery record once the authority anchored its terminal receipt."""
        if request.operation is not AuthorityOperation.CLEANUP:
            return
        binding = _activation_binding(request)
        receipt = await self._executor.run(
            lambda: self._coordinator.cleanup_quarantine(binding, request.plan_identity)
        )
        if receipt is None or receipt.managed:
            # An adopted receipt is the operator's; finalize leaves it in place (ADR-0787).
            return
        if (
            receipt.operation_identity != context.operation_identity
            or receipt.attempt_id != context.attempt_id
            or receipt.journal_sequence != context.journal_sequence
            or receipt.journal_digest != context.journal_digest
        ):
            raise ProviderRecoveryRefusal(
                "remote cleanup proof differs from the quarantine receipt"
            )
        await self._executor.run(lambda: self._coordinator.finalize_cleanup_quarantine(receipt))

    async def _quarantine_receipt(
        self, binding: RecoveryObjectBinding
    ) -> RemoteCleanupQuarantineV1 | None:
        if binding.kind != "recovery-record":
            raise ProviderRecoveryRefusal(
                "remote recovery-object port serves only the recovery record"
            )
        return await self._executor.run(
            lambda: self._coordinator.cleanup_quarantine_for_object(binding.reference)
        )

    async def observe_recovery_object(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef
    ) -> RecoveryObjectObservation:
        receipt = await self._quarantine_receipt(binding)
        if receipt is None:
            return RecoveryObjectObservation(
                binding=binding,
                present=False,
                managed=False,
                observed_digest=binding.mutation_journal_digest,
            )
        observation = quarantine_observation(receipt)
        if observation.binding != binding:
            raise ProviderRecoveryRefusal(
                "remote cleanup quarantine ownership conflicts with request"
            )
        return observation

    async def delete_recovery_object(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef, digest: str
    ) -> RecoveryObjectObservation:
        observed = await self.observe_recovery_object(binding, authority)
        if observed.observed_digest != digest or observed.managed:
            raise ProviderRecoveryRefusal(
                "remote cleanup quarantine observation changed before delete"
            )
        receipt = await self._quarantine_receipt(binding)
        if receipt is not None:
            if quarantine_observation(receipt).observed_digest != digest or receipt.managed:
                raise ProviderRecoveryRefusal(
                    "remote cleanup quarantine observation changed before delete"
                )
            await self._executor.run(lambda: self._coordinator.finalize_cleanup_quarantine(receipt))
        return await self.observe_recovery_object(binding, authority)

    async def adopt_recovery_object(
        self, binding: RecoveryObjectBinding, authority: OpaqueProviderRef, digest: str
    ) -> RecoveryObjectObservation:
        observed = await self.observe_recovery_object(binding, authority)
        receipt = await self._quarantine_receipt(binding)
        if (
            observed.observed_digest != digest
            or receipt is None
            or quarantine_observation(receipt).observed_digest != digest
        ):
            raise ProviderRecoveryRefusal(
                "remote cleanup quarantine observation changed before adopt"
            )
        await self._executor.run(lambda: self._coordinator.adopt_cleanup_quarantine(receipt))
        return await self.observe_recovery_object(binding, authority)

    async def _acknowledged_definition(
        self, request: AuthorityMutationRequestV1, point: RecoveryPoint
    ) -> AcknowledgedDefinition | None:
        """Return the third definition a conflict resolution acknowledged, if that is the state.

        The digest proves a fresh read is the acknowledged observation; the read's own digest
        pair is then the expectation the provider compares again at the write.
        """
        if not isinstance(request, AuthorityConflictResolutionRequestV1):
            return None
        state = await self._executor.run(lambda: self._coordinator.observe_state(point))
        if _composite_state(request, state) != request.expected_observed_composite:
            raise ProviderRecoveryRefusal(
                "remote conflict resolution observation changed before restore"
            )
        return (state.preserved, state.boot) if state.definition == "other" else None

    async def observe_running(
        self, request: AuthorityMutationRequestV1
    ) -> RunningKernelObservation:
        binding = _activation_binding(request)
        authority = authority_reference(request)

        def read() -> RunningKernelObservation:
            point = self._coordinator.recovery_point(binding, request.plan_identity)
            if (
                point.source_state.definition != request.expected_source_identity
                or point.target_state.definition != request.intended_target_identity
            ):
                raise ProviderRecoveryRefusal(
                    "remote running observation identities differ from request"
                )
            return self._coordinator.observe(point, authority)

        return await self._executor.run(read)


# Commit points whose provider effect is exactly one observation (ADR-0584).
_BOOKKEEPING_COMMITS = frozenset(
    {AuthorityOperation.DEADLINE, AuthorityOperation.FAIL, AuthorityOperation.RELEASE}
)


def _categorize(state: RemoteExternalBootObservedState, *, restored: bool) -> ObservationCategory:
    """Apply the adapter's documented module-half rule to one live read."""
    if state.definition == "source":
        return "absent" if state.recovery_objects_absent else "source"
    if state.definition == "target" and not state.recovery_objects_absent:
        return "mixed" if restored else "target"
    return "conflict"


def _composite_state(
    request: AuthorityMutationRequestV1,
    state: RemoteExternalBootObservedState | None,
) -> str:
    """Digest the observed state together with the activation it was observed on.

    A proof-of-observation token, as for local: it names the System, activation, Run and plan
    so two identically provisioned Systems never mint interchangeable tokens, and it carries
    only closed identifiers and digests, never provider output. The module restore evidence is
    deliberately not in it: conflict resolution writes that evidence before the authority
    rechecks the acknowledged composite, so including it would make every resolve stale.
    """
    payload = json.dumps(
        {
            "system_id": str(request.system_id),
            "activation_id": str(request.activation_id),
            "run_id": str(request.run_id),
            "plan_identity": request.plan_identity,
            "observed": None if state is None else asdict(state),
            "expected_source_identity": request.expected_source_identity,
            "intended_target_identity": request.intended_target_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return (
        "sha256:"
        + hashlib.sha256(b"kdive-remote-external-boot-composite-v1\0" + payload).hexdigest()
    )
