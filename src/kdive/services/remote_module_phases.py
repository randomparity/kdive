"""Crash-resumable phase orchestration for remote module recovery (ADR-0585)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.ports.authority import AuthorityRequestSender
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleRecoveryRefV2,
    RemoteModuleResultV1,
    identity_for,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_operation import (
    ModuleOperationRuntime,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)

type ResumeAction = Literal["install", "finish-install", "restore", "finish-restore"]

_PHASE_ACTIONS: dict[tuple[str, str], ResumeAction] = {
    ("capture_install", "captured"): "install",
    ("capture_install", "staging-intent"): "install",
    ("capture_install", "replacement-ready"): "install",
    ("capture_install", "installed"): "finish-install",
    ("restore", "installed"): "restore",
    ("restore", "restore-ready"): "restore",
    ("restore", "restored"): "finish-restore",
}


def classify_phase(operation: str, result: RemoteModuleResultV1) -> ResumeAction:
    """Map only a valid durable operation/result phase composite to its next action."""
    try:
        return _PHASE_ACTIONS[(operation, result.phase)]
    except KeyError as exc:
        raise CategorizedError(
            "unrecognized remote module phase composite",
            category=ErrorCategory.CONFLICT,
            details={"operation": operation, "phase": result.phase},
        ) from exc


@dataclass(frozen=True, slots=True)
class CaptureInstallRequest:
    preparation: ModuleAttemptPreparationRequestV1
    operation: RemoteModuleOperationV1
    authority: AuthorityRequestSender
    authority_reference: OpaqueProviderRef


def _validate_result(operation: RemoteModuleOperationV1, result: RemoteModuleResultV1) -> None:
    try:
        result.validate_for(operation)
    except ValueError as exc:
        raise CategorizedError(
            "remote module durable result differs from operation",
            category=ErrorCategory.CONFLICT,
        ) from exc
    if not result.is_identity_complete:
        raise CategorizedError(
            "remote module durable result lacks operation identity",
            category=ErrorCategory.CONFLICT,
        )
    if result.status == "failure":
        raise CategorizedError(
            "remote module appliance reported failure",
            category=ErrorCategory.INSTALL_FAILURE,
            details={
                "error_code": result.error_code,
                "phase": result.phase,
                "result_identity": identity_for(result),
            },
        )


async def capture_install_modules(
    request: CaptureInstallRequest,
    *,
    runtime: ModuleOperationRuntime,
    executor: RemoteModulePreparationExecutor,
    deadline: float,
) -> RemoteModuleRecoveryRefV2:
    """Converge capture/install and return only after installed evidence and teardown."""
    operation = request.operation
    if operation.operation != "capture_install":
        raise CategorizedError(
            "remote module capture requires capture_install operation",
            category=ErrorCategory.CONFLICT,
        )
    inspected = await runtime.inspect_attempt(request.preparation, operation, executor)
    if inspected is None:
        volumes = await runtime.prepare(
            request.preparation, operation, executor, request.authority, deadline
        )
        result = await runtime.run(operation, volumes, executor, deadline)
    else:
        volumes, result = inspected.volumes, inspected.result
        _validate_result(operation, result)
        if classify_phase(operation.operation, result) == "install":
            result = await runtime.run(operation, volumes, executor, deadline)
    _validate_result(operation, result)
    if classify_phase(operation.operation, result) != "finish-install":
        raise CategorizedError(
            "remote module install did not reach durable installed phase",
            category=ErrorCategory.CONFLICT,
        )
    if result.entry_count is None or result.content_bytes is None:
        raise CategorizedError(
            "remote module installed counts are absent",
            category=ErrorCategory.CONFLICT,
        )
    recovery = RemoteModuleRecoveryRefV2(
        system_id=operation.system_id,
        run_id=operation.run_id,
        plan_identity=operation.plan_identity,
        operation_nonce=operation.operation_nonce,
        pool=OpaqueProviderRef(ref=volumes.source.pool),
        root_volume=OpaqueProviderRef(ref=operation.root_volume.key),
        source_volume=OpaqueProviderRef(ref=volumes.source.name),
        scratch_volume=OpaqueProviderRef(ref=volumes.scratch.name),
        source_capacity_bytes=volumes.source.capacity_bytes,
        operation_identity=identity_for(operation),
        result_identity=identity_for(result),
        installed_entry_count=result.entry_count,
        installed_content_bytes=result.content_bytes,
        appliance_image_digest=operation.appliance_image_digest,
        authority_identity=RemoteModuleRecoveryRefV2.identity_for_authority(
            request.authority_reference
        ),
    )
    teardown = await runtime.teardown(recovery, executor, deadline)
    if not teardown.complete:
        raise CategorizedError(
            "remote module appliance teardown incomplete",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"teardown_complete": False},
        )
    await runtime.delete_source(recovery, executor)
    return recovery
