"""Provider contracts and fixed configuration for remote module operations (ADR-0588)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.infra.reaping import ModuleVolumeKey
from kdive.providers.ports.authority import AuthorityRequestSender
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance import (
    ApplianceConn,
    DeadlineExecutor,
    TeardownObservation,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    AttachmentInspection,
    RemoteDeviceIdentityPort,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleResultV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleRecoveryRefV2 as RemoteModuleRecoveryRefV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    FilesystemImageWriter,
    ModuleTreeEntry,
    PreparedModuleVolumes,
    PreparedVolume,
    StorageConn,
)
from kdive.security.secrets.secret_registry import SecretRegistry


class ModuleOperationRuntime(Protocol):
    async def inspect_attempt(
        self,
        request: ModuleAttemptPreparationRequestV1,
        operation: RemoteModuleOperationV1,
        executor: RemoteModulePreparationExecutor,
    ) -> ModuleAttemptInspection | None: ...
    async def reap_state(
        self, recovery: RemoteModuleRecoveryRefV1, executor: RemoteModulePreparationExecutor
    ) -> Literal["absent", "reaping", "reaped"]: ...
    def recovery_volumes(
        self, operation: RemoteModuleOperationV1, recovery: RemoteModuleRecoveryRefV1
    ) -> PreparedModuleVolumes: ...
    async def reopen_operation(
        self, recovery: RemoteModuleRecoveryRefV1, deadline: float | None = None
    ) -> RemoteModuleOperationV1: ...
    async def reopen_result(
        self, recovery: RemoteModuleRecoveryRefV1, deadline: float | None = None
    ) -> RemoteModuleResultV1: ...
    async def reopen_capture_operation(
        self, recovery: RemoteModuleRecoveryRefV1, deadline: float | None = None
    ) -> RemoteModuleOperationV1: ...
    async def reopen_installed_result(
        self, recovery: RemoteModuleRecoveryRefV1, deadline: float | None = None
    ) -> RemoteModuleResultV1: ...
    async def prepare(
        self,
        request: ModuleAttemptPreparationRequestV1,
        operation: RemoteModuleOperationV1,
        executor: RemoteModulePreparationExecutor,
        authority: AuthorityRequestSender | None,
        deadline: float,
    ) -> PreparedModuleVolumes: ...
    async def run(
        self,
        operation: RemoteModuleOperationV1,
        volumes: PreparedModuleVolumes,
        executor: RemoteModulePreparationExecutor,
        deadline: float,
    ) -> RemoteModuleResultV1: ...
    async def teardown(
        self,
        recovery: RemoteModuleRecoveryRefV1,
        executor: RemoteModulePreparationExecutor,
        deadline: float,
    ) -> TeardownObservation: ...
    async def delete_source(
        self, recovery: RemoteModuleRecoveryRefV1, executor: RemoteModulePreparationExecutor
    ) -> None: ...
    async def delete_scratch(
        self, recovery: RemoteModuleRecoveryRefV1, executor: RemoteModulePreparationExecutor
    ) -> None: ...
    async def record_reaping(
        self, recovery: RemoteModuleRecoveryRefV1, executor: RemoteModulePreparationExecutor
    ) -> None: ...
    async def record_reaped(
        self, recovery: RemoteModuleRecoveryRefV1, executor: RemoteModulePreparationExecutor
    ) -> None: ...
    async def resume_reap(
        self,
        recovery: RemoteModuleRecoveryRefV1,
        executor: RemoteModulePreparationExecutor,
        deadline: float,
    ) -> TeardownObservation: ...
    async def inventory(
        self, executor: RemoteModulePreparationExecutor
    ) -> tuple[ModuleVolumeKey, ...]: ...
    async def reap(self, retained: Callable[[], Awaitable[Collection[ModuleVolumeKey]]]) -> int: ...


@dataclass(frozen=True, slots=True)
class ModuleAttemptInspection:
    """Validated current-attempt volumes and their exact durable scratch result."""

    volumes: PreparedModuleVolumes
    result: RemoteModuleResultV1


@dataclass(frozen=True, slots=True)
class RemoteModuleVolumePreparation:
    storage: StorageConn
    pool_name: str
    entries: tuple[ModuleTreeEntry, ...]
    writer: FilesystemImageWriter
    inspect_attachments: Callable[[RemoteDeviceIdentityPort], AttachmentInspection]
    work_dir: Path


@dataclass(frozen=True, slots=True)
class RemoteModuleVolumeRecovery:
    storage: StorageConn
    pool_name: str


@dataclass(frozen=True, slots=True)
class RemoteModuleApplianceExecution:
    appliance: ApplianceConn
    architecture: str
    emulator_path: str
    memory_kib: int
    vcpus: int
    appliance_volume: str
    appliance_image_digest: str
    root: Callable[[RemoteModuleOperationV1], PreparedVolume]
    read_scratch_result: Callable[[PreparedVolume], bytes | None]
    inspect_attachments: Callable[[], AttachmentInspection]
    secret_registry: SecretRegistry
    deadline_executor: DeadlineExecutor
    monotonic: Callable[[], float]
