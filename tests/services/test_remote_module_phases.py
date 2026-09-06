"""Durable remote-module phase classification."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.infra.reaping import ModuleVolumeKey
from kdive.providers.ports.authority import AuthorityRequestSender
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance import (
    TeardownObservation,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleResultV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_operation import (
    ModuleAttemptInspection,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    PreparedModuleVolumes,
    PreparedVolume,
)
from kdive.services.remote_module_phases import (
    CaptureInstallRequest,
    capture_install_modules,
    classify_phase,
    inventory_module_attempts,
    reap_module_attempt,
    restore_modules,
)


def _result(phase: str) -> RemoteModuleResultV1:
    values: dict[str, Any] = {
        "status": "success",
        "phase": phase,
        "system_id": "12345678-1234-4234-8234-123456789abc",
        "run_id": "87654321-4321-4321-8321-cba987654321",
        "plan_identity": "sha256:" + "1" * 64,
        "operation_nonce": "2" * 32,
        "appliance_image_digest": "sha256:" + "3" * 64,
        "release": "6.12.0",
        "root_volume_key": "root",
        "root_volume_identity": "sha256:" + "4" * 64,
        "source_manifest": "sha256:" + "5" * 64,
        "capture_manifest": "sha256:" + "6" * 64,
    }
    if phase == "captured":
        values.update(entry_count=1, content_bytes=10)
    elif phase in {"replacement-ready", "installed"}:
        values.update(
            installed_manifest="sha256:" + "7" * 64,
            entry_count=1,
            content_bytes=10,
        )
    elif phase in {"restore-ready", "restored"}:
        values["installed_manifest"] = "sha256:" + "7" * 64
    return RemoteModuleResultV1.model_validate(values)


@pytest.mark.parametrize(
    ("operation", "phase", "action"),
    [
        ("capture_install", "captured", "install"),
        ("capture_install", "staging-intent", "install"),
        ("capture_install", "replacement-ready", "install"),
        ("capture_install", "installed", "finish-install"),
        ("restore", "installed", "restore"),
        ("restore", "restore-ready", "restore"),
        ("restore", "restored", "finish-restore"),
    ],
)
def test_classify_phase_maps_only_resumable_composites(
    operation: str, phase: str, action: str
) -> None:
    assert classify_phase(operation, _result(phase)) == action


def test_classify_phase_rejects_cross_operation_composite() -> None:
    with pytest.raises(CategorizedError) as caught:
        classify_phase("restore", _result("captured"))

    assert caught.value.category is ErrorCategory.CONFLICT
    assert caught.value.details == {"operation": "restore", "phase": "captured"}


def _operation() -> RemoteModuleOperationV1:
    return RemoteModuleOperationV1.model_validate(
        {
            "operation": "capture_install",
            "system_id": "12345678-1234-4234-8234-123456789abc",
            "run_id": "87654321-4321-4321-8321-cba987654321",
            "plan_identity": "sha256:" + "1" * 64,
            "operation_nonce": "2" * 32,
            "release": "6.12.0",
            "root_volume": {"key": "root", "identity": "sha256:" + "4" * 64},
            "source_manifest": "sha256:" + "5" * 64,
            "appliance_image_digest": "sha256:" + "3" * 64,
        }
    )


def _request(operation: RemoteModuleOperationV1) -> CaptureInstallRequest:
    preparation = ModuleAttemptPreparationRequestV1.model_validate(
        {
            "module_attempt_obligation": {
                "system_id": UUID(operation.system_id),
                "run_id": UUID(operation.run_id),
                "operation_nonce": operation.operation_nonce,
            }
        }
    )
    return CaptureInstallRequest(
        preparation,
        operation,
        cast(AuthorityRequestSender, object()),
        OpaqueProviderRef(ref="authority/fixed"),
    )


class Runtime:
    def __init__(self, result: RemoteModuleResultV1) -> None:
        self.result = result
        self.capture = _operation()
        self.current_operation = self.capture
        self.reap = "absent"
        self.calls: list[str] = []
        self.volumes = PreparedModuleVolumes(
            PreparedVolume(
                "pool",
                "source",
                result.system_id or "",
                result.run_id or "",
                result.operation_nonce or "",
                "source",
                result.source_manifest or "",
                4096,
            ),
            PreparedVolume(
                "pool",
                "scratch",
                result.system_id or "",
                result.run_id or "",
                result.operation_nonce or "",
                "scratch",
                "sha256:" + "0" * 64,
                10 * 1024**3,
            ),
        )

    async def inspect_attempt(self, *_args: object) -> ModuleAttemptInspection:
        self.calls.append("inspect")
        return ModuleAttemptInspection(self.volumes, self.result)

    async def run(self, operation: RemoteModuleOperationV1, *_args: object) -> RemoteModuleResultV1:
        self.calls.append("run")
        self.current_operation = operation
        self.result = _result(
            "installed" if operation.operation == "capture_install" else "restored"
        )
        return self.result

    async def teardown(self, *_args: object) -> TeardownObservation:
        self.calls.append("teardown")
        return TeardownObservation(True, True, True, True)

    async def delete_source(self, *_args: object) -> None:
        self.calls.append("delete-source")

    async def delete_scratch(self, *_args: object) -> None:
        self.calls.append("delete-scratch")

    async def reap_state(self, *_args: object) -> str:
        self.calls.append("reap-state")
        return self.reap

    async def reopen_capture_operation(self, *_args: object) -> RemoteModuleOperationV1:
        self.calls.append("reopen-capture")
        return self.capture

    async def reopen_installed_result(self, *_args: object) -> RemoteModuleResultV1:
        self.calls.append("reopen-installed")
        return _result("installed")

    async def reopen_operation(self, *_args: object) -> RemoteModuleOperationV1:
        self.calls.append("reopen-operation")
        return self.current_operation

    async def reopen_result(self, *_args: object) -> RemoteModuleResultV1:
        self.calls.append("reopen-result")
        return self.result

    def recovery_volumes(self, *_args: object) -> PreparedModuleVolumes:
        self.calls.append("recovery-volumes")
        return self.volumes

    async def record_reaping(self, *_args: object) -> None:
        self.calls.append("record-reaping")
        self.reap = "reaping"

    async def record_reaped(self, *_args: object) -> None:
        self.calls.append("record-reaped")
        self.reap = "reaped"

    async def resume_reap(self, *_args: object) -> TeardownObservation:
        self.calls.append("resume-reap")
        return TeardownObservation(True, True, True, True)


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["captured", "staging-intent", "replacement-ready"])
async def test_capture_install_resumes_phase_and_returns_after_safe_teardown(phase: str) -> None:
    operation = _operation()
    runtime = Runtime(_result(phase))

    recovery = await capture_install_modules(
        _request(operation),
        runtime=cast(Any, runtime),
        executor=cast(Any, SimpleNamespace()),
        deadline=100.0,
    )

    assert runtime.calls == ["inspect", "run", "teardown", "delete-source"]
    assert recovery.source_capacity_bytes == 4096
    assert recovery.installed_entry_count == 1


@pytest.mark.anyio
async def test_capture_install_does_not_repeat_completed_install() -> None:
    operation = _operation()
    runtime = Runtime(_result("installed"))

    await capture_install_modules(
        _request(operation),
        runtime=cast(Any, runtime),
        executor=cast(Any, SimpleNamespace()),
        deadline=100.0,
    )

    assert runtime.calls == ["inspect", "teardown", "delete-source"]


@pytest.mark.anyio
async def test_restore_resumes_from_installed_and_commits_reap_before_deletion() -> None:
    operation = _operation()
    runtime = Runtime(_result("installed"))
    recovery = await capture_install_modules(
        _request(operation),
        runtime=cast(Any, runtime),
        executor=cast(Any, SimpleNamespace()),
        deadline=100.0,
    )
    runtime.calls.clear()

    result = await restore_modules(
        recovery,
        OpaqueProviderRef(ref="authority/fixed"),
        runtime=cast(Any, runtime),
        executor=cast(Any, SimpleNamespace()),
        deadline=100.0,
    )

    assert result.phase == "restored"
    assert runtime.calls == [
        "reap-state",
        "reopen-capture",
        "reopen-installed",
        "reopen-operation",
        "reopen-result",
        "recovery-volumes",
        "run",
        "teardown",
        "record-reaping",
        "delete-source",
        "delete-scratch",
        "record-reaped",
    ]


@pytest.mark.anyio
async def test_inventory_uses_only_owned_keys_and_marks_unreadable_incomplete() -> None:
    retained = ModuleVolumeKey(
        "12345678-1234-4234-8234-123456789abc",
        "87654321-4321-4321-8321-cba987654321",
        "2" * 32,
        "source.ext4",
    )
    drainable = ModuleVolumeKey(retained.system_id, retained.run_id, "3" * 32, "scratch.ext4")

    class Readable:
        async def inventory(self, _executor: object) -> tuple[ModuleVolumeKey, ...]:
            return retained, drainable

    class Unreadable:
        async def inventory(self, _executor: object) -> tuple[ModuleVolumeKey, ...]:
            raise OSError("provider unavailable")

    inventory = await inventory_module_attempts(
        (cast(Any, Readable()), cast(Any, Unreadable())),
        {retained},
        cast(Any, SimpleNamespace()),
    )

    assert [(item.key, item.state) for item in inventory.items] == [
        (retained, "retained"),
        (drainable, "drainable"),
    ]
    assert inventory.complete is False
    assert inventory.rollback_safe is False


@pytest.mark.anyio
async def test_reap_resumes_from_durable_marker_without_repeating_teardown() -> None:
    operation = _operation()
    runtime = Runtime(_result("installed"))
    recovery = await capture_install_modules(
        _request(operation),
        runtime=cast(Any, runtime),
        executor=cast(Any, SimpleNamespace()),
        deadline=100.0,
    )
    await restore_modules(
        recovery,
        OpaqueProviderRef(ref="authority/fixed"),
        runtime=cast(Any, runtime),
        executor=cast(Any, SimpleNamespace()),
        deadline=100.0,
    )
    runtime.reap = "reaping"
    runtime.calls.clear()

    await reap_module_attempt(
        recovery,
        OpaqueProviderRef(ref="authority/fixed"),
        runtime=cast(Any, runtime),
        executor=cast(Any, SimpleNamespace()),
        deadline=100.0,
    )

    assert runtime.calls == [
        "reap-state",
        "resume-reap",
        "delete-source",
        "delete-scratch",
        "record-reaped",
    ]
