"""Worker-side verified remote provider-host preparation."""

from __future__ import annotations

import asyncio
import logging
import math
import random
from collections.abc import Awaitable, Callable
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
    AuthorityPreparationMutationRequestV1,
)
from kdive.providers.ports.authority import AuthorityRequestSender
from kdive.providers.ports.module_operation import (
    ModuleAuthority,
    ModuleBeginRequest,
    ModuleCompletion,
    ModuleLifecycleRequest,
    ModulePreparationRequest,
    PreparationExecutor,
    RemoteDeviceIdentityPort,
)
from kdive.services.remote_module_volume_preparation import (
    prepare_verified_remote_module_attempt,
)

_LOGGER = logging.getLogger(__name__)
_WAIT_MIN_S = 0.1
_WAIT_MAX_S = 2.0
_WAIT_GROWTH = 1.6
_WAIT_JITTER = 0.25
_WAIT_LOG_INTERVAL_S = 30.0


class RemoteModulePreparationAuthority(AuthorityRequestSender, ModuleAuthority, Protocol):
    """The worker's typed authority sender and provider module capability."""


@dataclass(frozen=True, slots=True)
class RemoteModulePreparationInputs:
    authority: AuthorityPreparationMutationRequestV1


def _terminal_evidence(
    response: ModuleCompletion,
) -> ModuleAttemptTerminalEvidence:
    typed_operation = response.operation
    recovery = response.recovery
    if recovery.installed_entry_count is None or recovery.installed_content_bytes is None:
        raise ValueError("remote module lifecycle recovery lacks installed counts")
    return ModuleAttemptTerminalEvidence(
        terminal_operation=typed_operation.evidence.document,
        terminal_operation_identity=typed_operation.evidence.identity,
        terminal_result=response.result.document,
        terminal_result_identity=response.result.identity,
        baseline_operation_identity=recovery.operation_identity,
        baseline_result_identity=recovery.result_identity,
        installed_entry_count=recovery.installed_entry_count,
        installed_content_bytes=recovery.installed_content_bytes,
        recovery_reference=recovery.document,
    )


def _restored_evidence(
    response: ModuleCompletion,
) -> ModuleAttemptRestoredEvidence:
    return ModuleAttemptRestoredEvidence(
        restored_operation=response.operation.evidence.document,
        restored_operation_identity=response.operation.evidence.identity,
        restored_result=response.result.document,
        restored_result_identity=response.result.identity,
    )


async def prepare_remote_module_on_authority_host(
    *,
    pool: AsyncConnectionPool,
    repository: RemoteModuleAttemptObligationRepository,
    sender: RemoteModulePreparationAuthority,
    inputs: RemoteModulePreparationInputs,
    executor: PreparationExecutor | None,
    job_id: UUID,
    job_attempt: int,
    incarnation_credential: SecretStr,
    deadline: float,
) -> ModuleCompletion:
    """Open exact server evidence, then retain worker verification through remote completion."""
    remaining = deadline - asyncio.get_running_loop().time()
    budget_seconds = min(900, math.floor(remaining))
    if budget_seconds < 1:
        raise TimeoutError("remote module preparation deadline expired")
    begin = await sender.open_remote_module_attempt(
        ModuleBeginRequest(
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
    remote_request = ModulePreparationRequest(
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
    ) -> ModuleCompletion:
        del identity
        if attempt != expected:
            raise ValueError("remote module verified attempt changed")
        check_deadline()
        return await await_authority_dispatch(
            lambda transport_deadline: sender.execute_remote_module_preparation(
                remote_request, deadline=transport_deadline
            ),
            deadline=deadline,
            what="remote module preparation",
        )

    async def commit_result(
        connection: AsyncConnection,
        attempt: ModuleAttempt,
        result: ModuleCompletion,
    ) -> None:
        evidence = _terminal_evidence(result)
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


def _unknown_completion(exc: CategorizedError, message: str) -> CategorizedError:
    """End a wait without claiming the dispatch did not run (ADR-0808)."""
    return CategorizedError(
        f"{exc}; {message}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={**exc.details, "completion": "unknown"},
    )


async def await_authority_dispatch[Value](
    dispatch: Callable[[float], Awaitable[Value]],
    *,
    deadline: float,
    what: str,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random.random,  # noqa: S311 - retry jitter, not security
) -> Value:
    """Dispatch one authority request until it settles, without abandoning an ambiguous one.

    A transport loss after the send may have admitted the request, so it is retried: the
    authority answers a replay from its journal. A definite rejection (``admission: refused``)
    proves this request was not admitted. On first sight it ends the wait as
    ``refused-before-mutation``. After an earlier ambiguity it ends as ``completion: unknown``,
    because the earlier dispatch may have run. A journal conflict ends the wait as ``unknown``
    too, since some follow a mutation. An ambiguous dispatch backs off from 0.1 s to 2 s and
    ends at ``deadline`` as ``unknown`` (ADR-0808). A cancellation never abandons the wait.
    """
    now = clock or asyncio.get_running_loop().time
    indeterminate = False
    attempts = 0
    started = now()
    last_logged = float("-inf")
    last_error = ""

    def note(error: BaseException) -> None:
        nonlocal last_logged, last_error
        moment = now()
        described = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
        if described != last_error or moment - last_logged >= _WAIT_LOG_INTERVAL_S:
            last_logged, last_error = moment, described
            _LOGGER.warning(
                "authority dispatch for %s is not settled: %s (attempt %d, %.1fs)",
                what,
                described,
                attempts,
                moment - started,
            )

    async def wait_before_retry() -> None:
        delay = min(_WAIT_MAX_S, _WAIT_MIN_S * _WAIT_GROWTH ** (attempts - 1))
        delay *= 1 + _WAIT_JITTER * jitter()
        delay = min(delay, max(0.0, deadline - now()))
        while True:
            try:
                await sleep(delay)
                return
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()

    while True:
        attempts += 1
        try:
            return await dispatch(max(deadline, now() + 5.0))
        except CategorizedError as exc:
            if (
                exc.category is ErrorCategory.CONFLICT
                and exc.details.get("completion") == "failed-after-mutation"
            ):
                raise
            admission = exc.details.get("admission")
            if admission == "refused" and not indeterminate:
                _LOGGER.warning("authority rejected %s: %s", what, exc)
                raise CategorizedError(
                    str(exc),
                    category=exc.category,
                    details={**exc.details, "completion": "refused-before-mutation"},
                ) from None
            if admission in {"refused", "unknown"}:
                _LOGGER.warning("authority rejected %s after an ambiguous dispatch: %s", what, exc)
                raise _unknown_completion(exc, "the dispatch may already have run") from None
            if not indeterminate and exc.category is not ErrorCategory.INFRASTRUCTURE_FAILURE:
                raise
            error: BaseException = exc
        except TimeoutError as exc:
            error = exc
        except asyncio.CancelledError as exc:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            error = exc
        except Exception as exc:  # noqa: BLE001 - no later error proves an ambiguous dispatch stopped
            if not indeterminate:
                raise
            error = exc
        indeterminate = True
        note(error)
        if now() >= deadline:
            raise CategorizedError(
                f"authority dispatch for {what} did not settle before its deadline",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"completion": "unknown"},
            )
        await wait_before_retry()


# A reap after a restore compares the host's completion with the retained restored evidence;
# only a System teardown reaps the PREP baseline.
_RESTORED_REAP_OPERATIONS = frozenset({"cleanup", "resolve-conflict"})


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
) -> ModuleCompletion:
    """Retain the System verifier until authenticated completion and fenced evidence commit."""
    remaining = deadline - asyncio.get_running_loop().time()
    budget_seconds = min(300, math.floor(remaining))
    if budget_seconds < 1:
        raise TimeoutError("remote module lifecycle deadline expired before dispatch")
    request = ModuleLifecycleRequest(
        authority=authority,
        action=action,
        budget_seconds=budget_seconds,
    )
    receipt = preparation.module_attempt_obligation
    attempt = ModuleAttempt(receipt.system_id, receipt.run_id, receipt.operation_nonce)
    retained_restored: ModuleAttemptRestoredEvidence | None = None
    retained_terminal: ModuleAttemptTerminalEvidence | None = None
    retry_after_discharge = False
    if action == "reap" and not await repository.reap_obligation_is_open(connection, attempt):
        # A CLEANUP retried after its reap already discharged the obligation (the worker's
        # commit_cleanup failed afterwards) reaches the host, whose reap is idempotent, and must
        # come back with the volumes reaped; no other reap proceeds without a retained obligation.
        if (
            authority.operation.value not in _RESTORED_REAP_OPERATIONS
            or not await repository.reap_obligation_is_discharged(connection, attempt)
        ):
            raise CategorizedError(
                "remote module reap obligation is not retained",
                category=ErrorCategory.CONFLICT,
            )
        retry_after_discharge = True
    if action == "reap" and authority.operation.value in _RESTORED_REAP_OPERATIONS:
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

    async def observe_completion() -> ModuleCompletion:
        return await await_authority_dispatch(
            lambda transport_deadline: sender.execute_remote_module_lifecycle(
                request, deadline=transport_deadline
            ),
            deadline=deadline,
            what=f"remote module {action}",
        )

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
        if retry_after_discharge and not response.volumes_absent:
            raise CategorizedError(
                "remote module cleanup retry needs the host to report the volumes reaped",
                category=ErrorCategory.CONFLICT,
            )
        if authority.operation.value in _RESTORED_REAP_OPERATIONS:
            if retained_restored != _restored_evidence(response):
                raise CategorizedError(
                    "remote module cleanup completion differs from restored evidence",
                    category=ErrorCategory.CONFLICT,
                )
        else:
            if retained_terminal != _terminal_evidence(response):
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
