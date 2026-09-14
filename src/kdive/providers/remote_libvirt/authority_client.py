"""Remote-module wire codecs behind the resource-bound authority capability (ADR-0654)."""

from kdive.providers.ports.authority import AuthorityRequestSender
from kdive.providers.ports.module_operation import (
    ModuleBeginRequest,
    ModuleBeginResponse,
    ModuleCompletion,
    ModuleDocument,
    ModuleLifecycleRequest,
    ModuleOperation,
    ModulePreparationRequest,
    ModuleRecovery,
)
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteModuleLifecycleRequestV1,
    RemoteModuleLifecycleResponseV1,
    RemoteModulePreparationBeginRequestV1,
    RemoteModulePreparationBeginResponseV1,
    RemoteModuleTerminalPreparationResponseV1,
    RemoteModuleVolumePreparationRequestV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    identity_for,
)


def module_operation(operation: RemoteModuleOperationV1) -> ModuleOperation:
    return ModuleOperation(
        system_id=operation.system_id,
        run_id=operation.run_id,
        plan_identity=operation.plan_identity,
        operation_nonce=operation.operation_nonce,
        source_manifest=operation.source_manifest,
        release=operation.release,
        evidence=ModuleDocument(operation.model_dump(mode="json"), identity_for(operation)),
    )


def module_completion(
    operation: RemoteModuleOperationV1,
    response: RemoteModuleTerminalPreparationResponseV1 | RemoteModuleLifecycleResponseV1,
) -> ModuleCompletion:
    recovery = response.recovery
    return ModuleCompletion(
        operation=module_operation(operation),
        result=ModuleDocument(
            response.result.model_dump(mode="json"), identity_for(response.result)
        ),
        recovery=ModuleRecovery(
            system_id=recovery.system_id,
            run_id=recovery.run_id,
            plan_identity=recovery.plan_identity,
            operation_nonce=recovery.operation_nonce,
            operation_identity=recovery.operation_identity,
            result_identity=recovery.result_identity,
            installed_entry_count=recovery.installed_entry_count,
            installed_content_bytes=recovery.installed_content_bytes,
            document=recovery.model_dump(mode="json"),
        ),
        action=response.action if isinstance(response, RemoteModuleLifecycleResponseV1) else None,
        volumes_absent=(
            response.volumes_absent
            if isinstance(response, RemoteModuleLifecycleResponseV1)
            else False
        ),
    )


class RemoteModuleAuthorityAdapter:
    """Validate provider records and expose only the evidence consumed by shared services."""

    def __init__(self, sender: AuthorityRequestSender) -> None:
        self._sender = sender

    async def open_remote_module_attempt(
        self, request: ModuleBeginRequest, *, deadline: float
    ) -> ModuleBeginResponse:
        response = await self._sender._request(
            "begin-remote-module-preparation",
            RemoteModulePreparationBeginRequestV1(
                authority=request.authority, budget_seconds=request.budget_seconds
            ),
            RemoteModulePreparationBeginResponseV1,
            deadline=deadline,
        )
        return ModuleBeginResponse(response.preparation, module_operation(response.operation))

    async def execute_remote_module_preparation(
        self, request: ModulePreparationRequest, *, deadline: float
    ) -> ModuleCompletion:
        operation = RemoteModuleOperationV1.model_validate(request.operation.evidence.document)
        response = await self._sender._request(
            "execute-remote-module-preparation",
            RemoteModuleVolumePreparationRequestV1(
                authority=request.authority, operation=operation
            ),
            RemoteModuleTerminalPreparationResponseV1,
            deadline=deadline,
        )
        response.validate_terminal_for(operation, request.authority)
        return module_completion(operation, response)

    async def execute_remote_module_lifecycle(
        self, request: ModuleLifecycleRequest, *, deadline: float
    ) -> ModuleCompletion:
        response = await self._sender._request(
            "execute-remote-module-lifecycle",
            RemoteModuleLifecycleRequestV1(
                authority=request.authority,
                action=request.action,
                budget_seconds=request.budget_seconds,
            ),
            RemoteModuleLifecycleResponseV1,
            deadline=deadline,
        )
        return module_completion(response.operation, response)
