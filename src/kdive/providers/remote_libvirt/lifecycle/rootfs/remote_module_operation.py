"""Provider contracts and fixed configuration for remote module operations (ADR-0588)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kdive.providers.ports.external_boot import ExternalBootArtifactStager
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance import (
    ApplianceConn,
    DeadlineExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    AttachmentInspection,
    RemoteDeviceIdentityPort,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    FilesystemImageWriter,
    ModuleTreeEntry,
    PreparedVolume,
    StorageConn,
)
from kdive.security.secrets.secret_registry import SecretRegistry


class PartialAttachmentInspector(Protocol):
    def __call__(
        self,
        identity_port: RemoteDeviceIdentityPort,
        present_attempt_volumes: frozenset[str] | None = None,
    ) -> AttachmentInspection: ...


@dataclass(frozen=True, slots=True)
class RemoteModuleVolumePreparation:
    storage: StorageConn
    pool_name: str
    entries: tuple[ModuleTreeEntry, ...]
    writer: FilesystemImageWriter
    inspect_attachments: PartialAttachmentInspector
    work_dir: Path
    artifact_stager: ExternalBootArtifactStager | None = None


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
    appliance_volume: str | None
    appliance_image_digest: str
    root: Callable[[RemoteModuleOperationV1], PreparedVolume]
    read_scratch_result: Callable[[PreparedVolume, float], bytes | None]
    inspect_attachments: Callable[[], AttachmentInspection]
    secret_registry: SecretRegistry
    deadline_executor: DeadlineExecutor
    monotonic: Callable[[], float]
    appliance_kernel: Path | None = None
    appliance_initrd: Path | None = None
    root_for_attempt: Callable[[str, str, str], PreparedVolume] | None = None
