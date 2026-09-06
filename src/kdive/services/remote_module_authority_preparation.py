"""Worker-side verified remote provider-host preparation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.external_boot_authority.protocol import AuthorityPreparationMutationRequestV1
from kdive.providers.ports.authority import AuthorityRequestSender
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteModuleTerminalPreparationResponseV1,
    RemoteModuleVolumePreparationRequestV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    RemoteDeviceIdentityPort,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.services.remote_module_volume_preparation import (
    prepare_verified_remote_module_attempt,
)


class RemoteModulePreparationAuthority(AuthorityRequestSender, Protocol):
    async def open_remote_module_attempt(
        self, request: AuthorityPreparationMutationRequestV1, *, deadline: float
    ) -> ModuleAttemptPreparationRequestV1: ...

    async def execute_remote_module_preparation(
        self, request: RemoteModuleVolumePreparationRequestV1, *, deadline: float
    ) -> RemoteModuleTerminalPreparationResponseV1: ...


@dataclass(frozen=True, slots=True)
class RemoteModulePreparationInputs:
    authority: AuthorityPreparationMutationRequestV1
    root_volume_key: str
    root_volume_identity: str
    appliance_image_digest: str


def _operation(
    inputs: RemoteModulePreparationInputs, preparation: ModuleAttemptPreparationRequestV1
) -> RemoteModuleOperationV1:
    authority = inputs.authority
    receipt = preparation.module_attempt_obligation
    return RemoteModuleOperationV1(
        operation="capture_install",
        system_id=str(receipt.system_id),
        run_id=str(receipt.run_id),
        plan_identity=authority.plan_identity,
        operation_nonce=receipt.operation_nonce,
        release=authority.plan.module_obligation.release,
        root_volume={"key": inputs.root_volume_key, "identity": inputs.root_volume_identity},
        source_manifest=authority.plan.module_obligation.source_manifest,
        appliance_image_digest=inputs.appliance_image_digest,
    )


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
    preparation = await sender.open_remote_module_attempt(inputs.authority, deadline=deadline)
    receipt = preparation.module_attempt_obligation
    expected = ModuleAttempt(receipt.system_id, receipt.run_id, receipt.operation_nonce)
    operation = _operation(inputs, preparation)
    remote_request = RemoteModuleVolumePreparationRequestV1(
        authority=inputs.authority,
        operation=operation,
        deadline=deadline,
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
        failure: CategorizedError | None = None
        for _ in range(2):
            try:
                result = await sender.execute_remote_module_preparation(
                    remote_request, deadline=deadline
                )
                result.validate_terminal_for(operation, inputs.authority)
                return result
            except CategorizedError as exc:
                if exc.category is not ErrorCategory.INFRASTRUCTURE_FAILURE:
                    raise
                failure = exc
                check_deadline()
        assert failure is not None
        raise failure

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
