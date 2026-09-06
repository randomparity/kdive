"""Crash-resumable phase orchestration for remote module recovery (ADR-0585)."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.infra.reaping import ModuleVolumeKey
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


@dataclass(frozen=True, slots=True)
class ModuleAttemptInventoryItem:
    key: ModuleVolumeKey
    state: Literal["retained", "drainable"]


@dataclass(frozen=True, slots=True)
class ModuleAttemptInventory:
    items: tuple[ModuleAttemptInventoryItem, ...]
    complete: bool

    @property
    def rollback_safe(self) -> bool:
        return self.complete and all(item.state == "drainable" for item in self.items)


async def inventory_module_attempts(
    runtimes: tuple[ModuleOperationRuntime, ...],
    retained: Collection[ModuleVolumeKey],
    executor: RemoteModulePreparationExecutor,
) -> ModuleAttemptInventory:
    """Classify only bounded whole-name-owned keys; unreadable inventories are incomplete."""
    items: list[ModuleAttemptInventoryItem] = []
    complete = True
    retained_keys = set(retained)
    for runtime in runtimes:
        try:
            observed = await runtime.inventory(executor)
        except Exception:
            complete = False
            continue
        items.extend(
            ModuleAttemptInventoryItem(key, "retained" if key in retained_keys else "drainable")
            for key in observed
        )
    return ModuleAttemptInventory(tuple(items), complete)


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


def _restore_operation(
    capture: RemoteModuleOperationV1, installed: RemoteModuleResultV1
) -> RemoteModuleOperationV1:
    if installed.phase != "installed" or installed.capture_state is None:
        raise CategorizedError(
            "remote module restore baseline is incomplete",
            category=ErrorCategory.CONFLICT,
        )
    return RemoteModuleOperationV1(
        operation="restore",
        system_id=capture.system_id,
        run_id=capture.run_id,
        plan_identity=capture.plan_identity,
        operation_nonce=capture.operation_nonce,
        release=capture.release,
        root_volume=capture.root_volume,
        source_manifest=capture.source_manifest,
        capture_manifest=installed.capture_manifest,
        capture_absent=installed.capture_absent,
        installed_manifest=installed.installed_manifest,
        appliance_image_digest=capture.appliance_image_digest,
    )


async def restore_modules(
    recovery: RemoteModuleRecoveryRefV2,
    authority_reference: OpaqueProviderRef,
    *,
    runtime: ModuleOperationRuntime,
    executor: RemoteModulePreparationExecutor,
    deadline: float,
) -> RemoteModuleResultV1:
    """Resume restoration and retain reap evidence before deleting either volume."""
    try:
        recovery.validate_authority(authority_reference)
    except ValueError as exc:
        raise CategorizedError(
            "remote module recovery authority differs",
            category=ErrorCategory.CONFLICT,
        ) from exc
    reap_state = await runtime.reap_state(recovery, executor)
    if reap_state != "absent":
        result = await runtime.reopen_result(recovery)
        if reap_state == "reaped":
            return result
        observation = await runtime.resume_reap(recovery, executor, deadline)
        if not observation.complete:
            raise CategorizedError(
                "remote module restore cleanup incomplete",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        await runtime.delete_source(recovery, executor)
        await runtime.delete_scratch(recovery, executor)
        await runtime.record_reaped(recovery, executor)
        return result

    capture = await runtime.reopen_capture_operation(recovery)
    installed = await runtime.reopen_installed_result(recovery)
    _validate_result(capture, installed)
    if (
        identity_for(capture) != recovery.operation_identity
        or identity_for(installed) != recovery.result_identity
    ):
        raise CategorizedError(
            "remote module restore baseline identity differs",
            category=ErrorCategory.CONFLICT,
        )
    restore = _restore_operation(capture, installed)
    current_operation = await runtime.reopen_operation(recovery)
    result = await runtime.reopen_result(recovery)
    if current_operation not in {capture, restore}:
        raise CategorizedError(
            "remote module current operation differs from restore baseline",
            category=ErrorCategory.CONFLICT,
        )
    _validate_result(current_operation, result)
    if classify_phase("restore", result) == "restore":
        result = await runtime.run(
            restore, runtime.recovery_volumes(restore, recovery), executor, deadline
        )
        _validate_result(restore, result)
    if classify_phase("restore", result) != "finish-restore":
        raise CategorizedError(
            "remote module restore did not reach durable restored phase",
            category=ErrorCategory.CONFLICT,
        )
    observation = await runtime.teardown(recovery, executor, deadline)
    if not observation.complete:
        raise CategorizedError(
            "remote module restore teardown incomplete",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
    await runtime.record_reaping(recovery, executor)
    await runtime.delete_source(recovery, executor)
    await runtime.delete_scratch(recovery, executor)
    await runtime.record_reaped(recovery, executor)
    return result
