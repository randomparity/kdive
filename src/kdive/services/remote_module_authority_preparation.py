"""Worker-side verified remote provider-host preparation."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    ModuleAttemptRestoredEvidence,
    ModuleAttemptTerminalEvidence,
    ModuleAttemptWorkerWriteContext,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityPreparationMutationRequestV1,
    AuthorityPreparationResponseV1,
)
from kdive.providers.ports.authority import AuthorityRequestSender
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteModuleLifecycleRequestV1,
    RemoteModuleLifecycleResponseV1,
    RemoteModulePreparationBeginRequestV1,
    RemoteModulePreparationBeginResponseV1,
    RemoteModuleTerminalPreparationResponseV1,
    RemoteModuleVolumePreparationRequestV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    RemoteDeviceIdentityPort,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    identity_for,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.services.remote_module_volume_preparation import (
    prepare_verified_remote_module_attempt,
)


class RemoteModulePreparationAuthority(AuthorityRequestSender, Protocol):
    async def observe_authority(
        self, request: AuthorityMutationRequestV1, *, deadline: float
    ) -> AuthorityObservationV1: ...

    async def execute_preparation(
        self, request: AuthorityPreparationMutationRequestV1, *, deadline: float
    ) -> AuthorityPreparationResponseV1: ...

    async def open_remote_module_attempt(
        self, request: RemoteModulePreparationBeginRequestV1, *, deadline: float
    ) -> RemoteModulePreparationBeginResponseV1: ...

    async def execute_remote_module_preparation(
        self, request: RemoteModuleVolumePreparationRequestV1, *, deadline: float
    ) -> RemoteModuleTerminalPreparationResponseV1: ...

    async def execute_remote_module_lifecycle(
        self, request: RemoteModuleLifecycleRequestV1, *, deadline: float
    ) -> RemoteModuleLifecycleResponseV1: ...


@dataclass(frozen=True, slots=True)
class RemoteModulePreparationInputs:
    authority: AuthorityPreparationMutationRequestV1


def _terminal_evidence(
    operation: RemoteModuleOperationV1,
    response: RemoteModuleTerminalPreparationResponseV1 | RemoteModuleLifecycleResponseV1,
) -> ModuleAttemptTerminalEvidence:
    typed_operation = (
        response.operation if isinstance(response, RemoteModuleLifecycleResponseV1) else operation
    )
    recovery = response.recovery
    if recovery.installed_entry_count is None or recovery.installed_content_bytes is None:
        raise ValueError("remote module lifecycle recovery lacks installed counts")
    return ModuleAttemptTerminalEvidence(
        terminal_operation=typed_operation.model_dump(mode="json"),
        terminal_operation_identity=identity_for(typed_operation),
        terminal_result=response.result.model_dump(mode="json"),
        terminal_result_identity=identity_for(response.result),
        baseline_operation_identity=recovery.operation_identity,
        baseline_result_identity=recovery.result_identity,
        installed_entry_count=recovery.installed_entry_count,
        installed_content_bytes=recovery.installed_content_bytes,
        recovery_reference=recovery.model_dump(mode="json"),
    )


def _restored_evidence(
    response: RemoteModuleLifecycleResponseV1,
) -> ModuleAttemptRestoredEvidence:
    return ModuleAttemptRestoredEvidence(
        restored_operation=response.operation.model_dump(mode="json"),
        restored_operation_identity=identity_for(response.operation),
        restored_result=response.result.model_dump(mode="json"),
        restored_result_identity=identity_for(response.result),
    )


async def prepare_remote_module_on_authority_host(
    *,
    pool: AsyncConnectionPool,
    repository: RemoteModuleAttemptObligationRepository,
    sender: RemoteModulePreparationAuthority,
    inputs: RemoteModulePreparationInputs,
    executor: RemoteModulePreparationExecutor,
    job_id: UUID,
    job_attempt: int,
    incarnation_credential: SecretStr,
    deadline: float,
) -> RemoteModuleTerminalPreparationResponseV1:
    """Open exact server evidence, then retain worker verification through remote completion."""
    remaining = deadline - asyncio.get_running_loop().time()
    budget_seconds = min(900, math.floor(remaining))
    if budget_seconds < 1:
        raise TimeoutError("remote module preparation deadline expired")
    begin = await sender.open_remote_module_attempt(
        RemoteModulePreparationBeginRequestV1(
            authority=inputs.authority,
            budget_seconds=budget_seconds,
        ),
        deadline=deadline,
    )
    preparation = begin.preparation
    receipt = preparation.module_attempt_obligation
    expected = ModuleAttempt(receipt.system_id, receipt.run_id, receipt.operation_nonce)
    operation = begin.operation
    authority = inputs.authority
    if (
        operation.system_id != str(authority.system_id)
        or operation.run_id != str(authority.run_id)
        or operation.plan_identity != authority.plan_identity
        or operation.source_manifest != authority.plan.module_obligation.source_manifest
        or operation.release != authority.plan.module_obligation.release
    ):
        raise ValueError("authority-derived remote module operation differs from worker plan")
    remote_request = RemoteModuleVolumePreparationRequestV1(
        authority=authority,
        operation=operation,
    )
    worker_context = ModuleAttemptWorkerWriteContext(
        job_id=job_id,
        job_attempt=job_attempt,
        incarnation_credential=incarnation_credential,
        preparation=preparation,
    )

    async def execute(
        attempt: ModuleAttempt,
        identity: RemoteDeviceIdentityPort,
        check_deadline: Callable[[], None],
    ) -> RemoteModuleTerminalPreparationResponseV1:
        del identity
        if attempt != expected:
            raise ValueError("remote module verified attempt changed")
        check_deadline()
        indeterminate = False

        async def wait_before_retry() -> None:
            while True:
                try:
                    await asyncio.sleep(0.1)
                    return
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None:
                        current.uncancel()
                    continue

        while True:
            try:
                transport_deadline = max(deadline, asyncio.get_running_loop().time() + 5.0)
                result = await sender.execute_remote_module_preparation(
                    remote_request, deadline=transport_deadline
                )
                result.validate_terminal_for(operation, authority)
                return result
            except CategorizedError as exc:
                terminal_failure = (
                    exc.category is ErrorCategory.CONFLICT
                    and exc.details.get("completion") == "failed-after-mutation"
                )
                if terminal_failure or (
                    not indeterminate and exc.category is not ErrorCategory.INFRASTRUCTURE_FAILURE
                ):
                    raise
                indeterminate = True
                await wait_before_retry()
            except TimeoutError:
                indeterminate = True
                await wait_before_retry()
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                indeterminate = True
                await wait_before_retry()
            except Exception:  # noqa: BLE001 - no later error proves an ambiguous dispatch stopped
                if not indeterminate:
                    raise
                await wait_before_retry()

    async def commit_result(
        connection: AsyncConnection,
        attempt: ModuleAttempt,
        result: RemoteModuleTerminalPreparationResponseV1,
    ) -> None:
        evidence = _terminal_evidence(operation, result)
        existing = await repository.read_terminal_evidence(connection, attempt)
        if existing is not None and existing != evidence:
            raise CategorizedError(
                "remote module PREP terminal evidence differs from retained evidence",
                category=ErrorCategory.CONFLICT,
            )
        if not await repository.worker_record_terminal_evidence(
            connection,
            worker_context,
            attempt,
            evidence,
        ):
            raise CategorizedError(
                "remote module PREP terminal evidence authority is stale",
                category=ErrorCategory.STALE_HANDLE,
            )

    return await prepare_verified_remote_module_attempt(
        pool,
        repository,
        preparation,
        expected,
        executor,
        sender,
        deadline,
        None,
        awaited_operation=execute,
        commit_result=commit_result,
        allow_terminal_replay=True,
    )


async def execute_remote_module_lifecycle_on_authority_host(
    *,
    connection: AsyncConnection,
    repository: RemoteModuleAttemptObligationRepository,
    sender: RemoteModulePreparationAuthority,
    authority: AuthorityMutationRequestV1,
    preparation: ModuleAttemptPreparationRequestV1,
    worker_context: ModuleAttemptWorkerWriteContext,
    action: Literal["restore", "reap"],
    deadline: float,
) -> RemoteModuleLifecycleResponseV1:
    """Retain the System verifier until authenticated completion and fenced evidence commit."""
    remaining = deadline - asyncio.get_running_loop().time()
    budget_seconds = min(300, math.floor(remaining))
    if budget_seconds < 1:
        raise TimeoutError("remote module lifecycle deadline expired before dispatch")
    request = RemoteModuleLifecycleRequestV1(
        authority=authority,
        action=action,
        budget_seconds=budget_seconds,
    )
    receipt = preparation.module_attempt_obligation
    attempt = ModuleAttempt(receipt.system_id, receipt.run_id, receipt.operation_nonce)
    retained_restored: ModuleAttemptRestoredEvidence | None = None
    retained_terminal: ModuleAttemptTerminalEvidence | None = None
    if action == "reap" and not await repository.reap_obligation_is_open(connection, attempt):
        raise CategorizedError(
            "remote module reap obligation is not retained",
            category=ErrorCategory.CONFLICT,
        )
    if action == "reap" and authority.operation.value == "cleanup":
        retained_restored = await repository.read_restored_evidence(connection, attempt)
        if retained_restored is None:
            raise CategorizedError(
                "remote module cleanup requires retained restored evidence",
                category=ErrorCategory.CONFLICT,
            )
    elif action == "reap":
        retained_terminal = await repository.read_terminal_evidence(connection, attempt)
        if retained_terminal is None:
            raise CategorizedError(
                "remote module teardown requires retained PREP evidence",
                category=ErrorCategory.CONFLICT,
            )

    async def observe_completion() -> RemoteModuleLifecycleResponseV1:
        indeterminate = False

        async def wait_before_retry() -> None:
            while True:
                try:
                    await asyncio.sleep(0.1)
                    return
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None:
                        current.uncancel()
                    continue

        while True:
            try:
                transport_deadline = max(deadline, asyncio.get_running_loop().time() + 5.0)
                response = await sender.execute_remote_module_lifecycle(
                    request, deadline=transport_deadline
                )
                return response
            except CategorizedError as exc:
                terminal_failure = (
                    exc.category is ErrorCategory.CONFLICT
                    and exc.details.get("completion") == "failed-after-mutation"
                )
                if terminal_failure or (
                    not indeterminate and exc.category is not ErrorCategory.INFRASTRUCTURE_FAILURE
                ):
                    raise
                indeterminate = True
                await wait_before_retry()
            except TimeoutError:
                indeterminate = True
                await wait_before_retry()
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                indeterminate = True
                await wait_before_retry()
            except Exception:  # noqa: BLE001 - no later error proves an ambiguous dispatch stopped
                if not indeterminate:
                    raise
                await wait_before_retry()

    completion = asyncio.create_task(observe_completion())
    caller = asyncio.current_task()
    assert caller is not None
    completed = asyncio.Event()
    completion.add_done_callback(lambda _task: completed.set())
    cancelled: asyncio.CancelledError | None = None
    consumed_cancellations = 0
    while not completed.is_set():
        try:
            await completed.wait()
        except asyncio.CancelledError as error:
            cancelled = cancelled or error
            consumed_cancellations += 1
            caller.uncancel()
    if cancelled is None and caller.cancelling() != 0:
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError as error:
            cancelled = error
            consumed_cancellations += 1
            caller.uncancel()
    if cancelled is not None:
        if not completion.cancelled():
            completion.exception()
        message = cancelled.args[0] if cancelled.args else None
        for _ in range(consumed_cancellations):
            caller.cancel(message)
        raise cancelled from None
    response = completion.result()
    if (
        response.action != action
        or response.recovery.system_id != str(receipt.system_id)
        or response.recovery.run_id != str(receipt.run_id)
        or response.recovery.operation_nonce != receipt.operation_nonce
        or response.recovery.plan_identity != authority.plan_identity
    ):
        raise ValueError("remote module lifecycle response differs from worker binding")
    if action == "restore":
        evidence = _restored_evidence(response)
        existing = await repository.read_restored_evidence(connection, attempt)
        if existing is not None and existing != evidence:
            raise CategorizedError(
                "remote module restored evidence differs from retained evidence",
                category=ErrorCategory.CONFLICT,
            )
        if not await repository.worker_record_restored_evidence(
            connection, worker_context, attempt, evidence
        ):
            raise CategorizedError(
                "remote module restored evidence authority is stale",
                category=ErrorCategory.STALE_HANDLE,
            )
    else:
        if authority.operation.value == "cleanup":
            if retained_restored != _restored_evidence(response):
                raise CategorizedError(
                    "remote module cleanup completion differs from restored evidence",
                    category=ErrorCategory.CONFLICT,
                )
        else:
            if retained_terminal != _terminal_evidence(response.operation, response):
                raise CategorizedError(
                    "remote module teardown completion differs from PREP evidence",
                    category=ErrorCategory.CONFLICT,
                )
        if not await repository.worker_discharge_reap_obligation(
            connection, worker_context, attempt
        ):
            raise CategorizedError(
                "remote module reap evidence authority is stale",
                category=ErrorCategory.STALE_HANDLE,
            )
    return response
