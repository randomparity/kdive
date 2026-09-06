"""Worker-side verified remote provider-host preparation."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority.protocol import (
    AuthorityPreparationMutationRequestV1,
    AuthorityPreparationResponseV1,
)
from kdive.providers.ports.authority import AuthorityRequestSender
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteModulePreparationBeginRequestV1,
    RemoteModulePreparationBeginResponseV1,
    RemoteModuleTerminalPreparationResponseV1,
    RemoteModuleVolumePreparationRequestV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    RemoteDeviceIdentityPort,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.services.remote_module_volume_preparation import (
    prepare_verified_remote_module_attempt,
)


class RemoteModulePreparationAuthority(AuthorityRequestSender, Protocol):
    async def execute_preparation(
        self, request: AuthorityPreparationMutationRequestV1, *, deadline: float
    ) -> AuthorityPreparationResponseV1: ...

    async def open_remote_module_attempt(
        self, request: RemoteModulePreparationBeginRequestV1, *, deadline: float
    ) -> RemoteModulePreparationBeginResponseV1: ...

    async def execute_remote_module_preparation(
        self, request: RemoteModuleVolumePreparationRequestV1, *, deadline: float
    ) -> RemoteModuleTerminalPreparationResponseV1: ...


@dataclass(frozen=True, slots=True)
class RemoteModulePreparationInputs:
    authority: AuthorityPreparationMutationRequestV1


async def prepare_remote_module_on_authority_host(
    *,
    pool: AsyncConnectionPool,
    repository: RemoteModuleAttemptObligationRepository,
    sender: RemoteModulePreparationAuthority,
    inputs: RemoteModulePreparationInputs,
    executor: RemoteModulePreparationExecutor,
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

    async def execute(
        attempt: ModuleAttempt,
        identity: RemoteDeviceIdentityPort,
        check_deadline: Callable[[], None],
    ) -> RemoteModuleTerminalPreparationResponseV1:
        del identity
        if attempt != expected:
            raise ValueError("remote module verified attempt changed")
        check_deadline()
        while True:
            try:
                transport_deadline = max(deadline, asyncio.get_running_loop().time() + 5.0)
                result = await sender.execute_remote_module_preparation(
                    remote_request, deadline=transport_deadline
                )
                result.validate_terminal_for(operation, authority)
                return result
            except CategorizedError as exc:
                if exc.category is not ErrorCategory.INFRASTRUCTURE_FAILURE:
                    raise
                await asyncio.sleep(0.1)

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
    )
