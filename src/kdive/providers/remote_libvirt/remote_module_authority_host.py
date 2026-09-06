"""Concrete provider-host remote-module preparation under one completion owner."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.external_boot_authority.protocol import AuthorityPreparationMutationRequestV1
from kdive.providers.ports.external_boot import ExternalBootArtifactStager, OpaqueProviderRef
from kdive.providers.remote_libvirt.external_boot_authority import (
    AdmittedRemoteModulePreparation,
    RemoteModuleTerminalPreparationResponseV1,
    RemoteModuleVolumePreparationRequestV1,
    RemoteModuleVolumePreparationResponseV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance import (
    ApplianceRequest,
    run_or_adopt_appliance,
    teardown_remote_module_appliance,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    AttachmentConn,
    AttachmentInspection,
    ExpectedAppliance,
    ExpectedAttachmentState,
    HostStatDeviceIdentity,
    RemoteDeviceIdentityPort,
    inspect_module_attachments,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleRecoveryRefV2,
    RemoteModuleResultV1,
    identity_for,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_operation import (
    RemoteModuleApplianceExecution,
    RemoteModuleVolumePreparation,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    CompletionDeadlineExecutor,
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_result_reader import (
    SparseRemoteModuleResultReader,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    Ext4SourceFilesystemWriter,
    PreparedModuleVolumes,
    PreparedVolume,
    VolumeRequest,
    prepare_attempt_volumes,
    render_module_volume_name,
)
from kdive.security.secrets.secret_registry import SecretRegistry


@dataclass(frozen=True, slots=True)
class RemoteModuleAuthorityHostConfiguration:
    """Fixed provider-host resources; no caller-selected connection, path, or executable."""

    volumes: RemoteModuleVolumePreparation
    appliance: RemoteModuleApplianceExecution
    identity: RemoteDeviceIdentityPort
    fixed_inspection: bool = False


@dataclass(frozen=True, slots=True)
class InstalledRemoteModuleAppliance:
    """Verified fixed direct-kernel appliance assets and manifest identity."""

    architecture: str
    kernel: Path
    initrd: Path
    manifest_digest: str


def _read_regular(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size > limit:
            raise ValueError("installed appliance file is not bounded regular content")
        data = bytearray()
        while chunk := os.read(descriptor, min(1024 * 1024, limit + 1 - len(data))):
            data.extend(chunk)
            if len(data) > limit:
                raise ValueError("installed appliance file exceeds its manifest bound")
        return bytes(data)
    finally:
        os.close(descriptor)


def _verify_regular_file(path: Path, size: int, digest: str) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size != size:
            raise ValueError("installed appliance file differs from its manifest")
        observed = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            observed.update(chunk)
        if observed.hexdigest() != digest:
            raise ValueError("installed appliance file differs from its manifest")
    finally:
        os.close(descriptor)


def load_installed_remote_module_appliance(
    root: Path, architecture: str
) -> InstalledRemoteModuleAppliance:
    """Reopen both installed assets and verify them against one canonical manifest."""
    directory = root / architecture
    manifest_bytes = _read_regular(directory / "manifest.json", 65_536)
    if not manifest_bytes.endswith(b"\n") or manifest_bytes.endswith(b"\n\n"):
        raise ValueError("installed appliance manifest is not newline framed")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError:
        raise ValueError("installed appliance manifest is invalid") from None
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"architecture", "files", "format", "initramfs_files"}
        or manifest.get("format") != "kdive-remote-module-appliance-v1"
        or manifest.get("architecture") != architecture
        or json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        != manifest_bytes
    ):
        raise ValueError("installed appliance manifest is not canonical or architecture-bound")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != 2:
        raise ValueError("installed appliance manifest has an invalid file set")
    expected = {
        "image/vmlinuz": directory / "image/vmlinuz",
        "image/initramfs.cpio": directory / "image/initramfs.cpio",
    }
    seen: set[str] = set()
    for record in files:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size_bytes"}:
            raise ValueError("installed appliance manifest has an invalid file record")
        path = record.get("path")
        digest = record.get("sha256")
        size = record.get("size_bytes")
        if (
            not isinstance(path, str)
            or path not in expected
            or path in seen
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or type(size) is not int
            or size < 1
            or size > 1024**3
        ):
            raise ValueError("installed appliance manifest file identity is invalid")
        _verify_regular_file(expected[path], size, digest)
        seen.add(path)
    if seen != set(expected):
        raise ValueError("installed appliance manifest omits a required file")
    return InstalledRemoteModuleAppliance(
        architecture=architecture,
        kernel=expected["image/vmlinuz"],
        initrd=expected["image/initramfs.cpio"],
        manifest_digest="sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
    )


class ConcreteRemoteModuleAuthorityHost:
    """Create both volumes, run/adopt the appliance, reopen its result, and tear it down."""

    def __init__(
        self,
        configuration: RemoteModuleAuthorityHostConfiguration,
        executor: RemoteModulePreparationExecutor,
    ) -> None:
        self._configuration = configuration
        self._executor = executor

    def _expected_attachments(self, operation: RemoteModuleOperationV1) -> ExpectedAttachmentState:
        appliance = self._configuration.appliance
        return ExpectedAttachmentState(
            system_id=operation.system_id,
            pool=self._configuration.volumes.pool_name,
            root_volume=operation.root_volume.key,
            source_volume=render_module_volume_name(
                operation.system_id, operation.run_id, operation.operation_nonce, "source.ext4"
            ),
            scratch_volume=render_module_volume_name(
                operation.system_id, operation.run_id, operation.operation_nonce, "scratch.ext4"
            ),
            appliance=ExpectedAppliance(
                name=(
                    f"kdive-module-{operation.system_id}-{operation.run_id}-"
                    f"{operation.operation_nonce}"
                ),
                architecture=appliance.architecture,
                image_digest=appliance.appliance_image_digest,
                operation_nonce=operation.operation_nonce,
                volume=appliance.appliance_volume,
                memory_kib=appliance.memory_kib,
                vcpus=appliance.vcpus,
                emulator_path=appliance.emulator_path,
                kernel=(
                    None if appliance.appliance_kernel is None else str(appliance.appliance_kernel)
                ),
                initrd=(
                    None if appliance.appliance_initrd is None else str(appliance.appliance_initrd)
                ),
            ),
        )

    def _inspect(
        self,
        operation: RemoteModuleOperationV1,
        present_attempt_volumes: frozenset[str] | None = None,
    ) -> AttachmentInspection:
        return inspect_module_attachments(
            cast(AttachmentConn, self._configuration.volumes.storage),
            self._configuration.identity,
            self._expected_attachments(operation),
            present_attempt_volumes,
        )

    async def derive_operation(
        self,
        authority: AuthorityPreparationMutationRequestV1,
        preparation: ModuleAttemptPreparationRequestV1,
    ) -> RemoteModuleOperationV1:
        """Derive every provider-owned operation field from fixed host resources."""
        receipt = preparation.module_attempt_obligation
        plan = authority.plan
        configured = self._configuration.appliance
        root_for_attempt = configured.root_for_attempt
        if root_for_attempt is None:
            raise CategorizedError(
                "remote module root derivation is not configured",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )

        def derive() -> RemoteModuleOperationV1:
            root = root_for_attempt(
                str(receipt.system_id), str(receipt.run_id), receipt.operation_nonce
            )
            return RemoteModuleOperationV1(
                operation="capture_install",
                system_id=str(receipt.system_id),
                run_id=str(receipt.run_id),
                plan_identity=authority.plan_identity,
                operation_nonce=receipt.operation_nonce,
                release=plan.module_obligation.release,
                root_volume={"key": root.name, "identity": root.digest},
                source_manifest=plan.module_obligation.source_manifest,
                appliance_image_digest=configured.appliance_image_digest,
            )

        return await self._executor.run(derive)

    def _volume_request(
        self, request: RemoteModuleVolumePreparationRequestV1, archive: Path
    ) -> VolumeRequest:
        operation = request.operation
        configured = self._configuration.volumes
        return VolumeRequest(
            pool=configured.pool_name,
            system_id=operation.system_id,
            run_id=operation.run_id,
            operation_nonce=operation.operation_nonce,
            operation=operation,
            source_manifest=operation.source_manifest,
            entries=configured.entries,
            writer=configured.writer,
            inspect_attachments=(
                lambda: (
                    self._inspect(operation)
                    if self._configuration.fixed_inspection
                    else configured.inspect_attachments(self._configuration.identity)
                )
            ),
            work_dir=configured.work_dir,
            archive=archive,
        )

    def _appliance_request(
        self,
        request: RemoteModuleVolumePreparationRequestV1,
        volumes: PreparedModuleVolumes,
        root: PreparedVolume,
        deadline: float,
    ) -> ApplianceRequest:
        operation = request.operation
        configured = self._configuration.appliance
        return ApplianceRequest(
            name=f"kdive-module-{operation.system_id}-{operation.run_id}-{operation.operation_nonce}",
            architecture=configured.architecture,
            emulator_path=configured.emulator_path,
            memory_kib=configured.memory_kib,
            vcpus=configured.vcpus,
            pool=self._configuration.volumes.pool_name,
            appliance_volume=configured.appliance_volume,
            appliance_kernel=(
                None if configured.appliance_kernel is None else str(configured.appliance_kernel)
            ),
            appliance_initrd=(
                None if configured.appliance_initrd is None else str(configured.appliance_initrd)
            ),
            appliance_image_digest=configured.appliance_image_digest,
            root=root,
            source=volumes.source,
            scratch=volumes.scratch,
            operation=operation,
            secret_registry=configured.secret_registry,
            read_scratch_result=lambda: configured.read_scratch_result(volumes.scratch, deadline),
            inspect_attachments=(
                lambda: (
                    self._inspect(operation)
                    if self._configuration.fixed_inspection
                    else configured.inspect_attachments()
                )
            ),
            executor=configured.deadline_executor,
            monotonic=configured.monotonic,
            invocation_deadline=deadline,
        )

    def _execute(
        self, admitted: AdmittedRemoteModulePreparation
    ) -> RemoteModuleTerminalPreparationResponseV1:
        request = admitted.request
        deadline = admitted.local_deadline
        operation = request.operation
        volumes_config = self._configuration.volumes
        appliance_config = self._configuration.appliance
        root = appliance_config.root(operation)
        if (
            appliance_config.appliance_image_digest != operation.appliance_image_digest
            or root.name != operation.root_volume.key
            or root.digest != operation.root_volume.identity
        ):
            raise CategorizedError(
                "remote module fixed appliance or root identity changed",
                category=ErrorCategory.CONFLICT,
            )
        stager = volumes_config.artifact_stager
        if stager is None:
            raise CategorizedError(
                "remote module artifact staging is not configured",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        with tempfile.TemporaryDirectory(
            prefix="kdive-remote-module-", dir=volumes_config.work_dir
        ) as raw:
            directory = Path(raw)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                evidence, _installed_manifest = stager.materialize_artifacts(
                    request.authority.plan, descriptor
                )
            finally:
                os.close(descriptor)
            if (
                evidence.get("release") != operation.release
                or evidence.get("module_source_manifest") != operation.source_manifest
            ):
                raise CategorizedError(
                    "remote module materialized source differs from operation",
                    category=ErrorCategory.CONFLICT,
                )
            volumes = prepare_attempt_volumes(
                volumes_config.storage,
                self._volume_request(request, directory / "modules"),
                admit_mutation=lambda: self._require_deadline(deadline),
            )
        appliance_request = self._appliance_request(request, volumes, root, deadline)
        outcome = run_or_adopt_appliance(self._configuration.appliance.appliance, appliance_request)
        if outcome.result is None:
            raise CategorizedError(
                "remote module appliance did not produce a durable result",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"timed_out": outcome.timed_out},
            )
        raw = self._configuration.appliance.read_scratch_result(volumes.scratch, deadline)
        if raw is None:
            raise CategorizedError(
                "remote module result artifact is absent", category=ErrorCategory.CONFLICT
            )
        durable = RemoteModuleResultV1.from_wire_bytes(raw)
        durable.validate_for(operation)
        if (
            durable != outcome.result
            or durable.entry_count is None
            or durable.content_bytes is None
        ):
            raise CategorizedError(
                "remote module terminal result changed or is incomplete",
                category=ErrorCategory.CONFLICT,
            )
        teardown = teardown_remote_module_appliance(
            self._configuration.appliance.appliance, appliance_request
        )
        if not teardown.complete:
            raise CategorizedError(
                "remote module appliance teardown is incomplete",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        authority = request.authority
        authority_reference = OpaqueProviderRef(
            ref=f"authority/{authority.authority_id}/{authority.generation}/{authority.attempt_id}"
        )
        base = RemoteModuleVolumePreparationResponseV1.from_prepared(volumes)
        response = RemoteModuleTerminalPreparationResponseV1(
            source=base.source,
            scratch=base.scratch,
            result=durable,
            recovery=RemoteModuleRecoveryRefV2(
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
                result_identity=identity_for(durable),
                installed_entry_count=durable.entry_count,
                installed_content_bytes=durable.content_bytes,
                appliance_image_digest=operation.appliance_image_digest,
                authority_identity=RemoteModuleRecoveryRefV2.identity_for_authority(
                    authority_reference
                ),
            ),
        )
        response.validate_terminal_for(operation, authority)
        return response

    def _require_deadline(self, deadline: float) -> None:
        if self._configuration.appliance.monotonic() >= deadline:
            raise TimeoutError("remote module provider deadline expired")

    async def execute(
        self, admitted: AdmittedRemoteModulePreparation
    ) -> RemoteModuleTerminalPreparationResponseV1:
        return await self._executor.run(lambda: self._execute(admitted))


class RemoteModuleAuthorityHostFactory:
    """Build request-specific typed operations from fixed authority-host dependencies."""

    def __init__(
        self,
        *,
        connection: Any,
        pool_name: str,
        work_dir: Path,
        appliance_root: Path,
        artifact_stager: ExternalBootArtifactStager,
        secret_registry: SecretRegistry,
        executor: RemoteModulePreparationExecutor,
        monotonic: Callable[[], float],
    ) -> None:
        self._connection = connection
        self._pool_name = pool_name
        self._work_dir = work_dir
        self._appliance_root = appliance_root
        self._artifact_stager = artifact_stager
        self._secret_registry = secret_registry
        self._executor = executor
        self._monotonic = monotonic
        self._identity = HostStatDeviceIdentity()
        self._deadline_executor = CompletionDeadlineExecutor(monotonic)
        self._reader = SparseRemoteModuleResultReader(
            storage=connection,
            work_dir=work_dir,
            executor=self._deadline_executor,
            monotonic=monotonic,
        )

    def _root(self, system_id: str, run_id: str, operation_nonce: str) -> PreparedVolume:
        from kdive.providers.remote_libvirt.lifecycle.xml import overlay_volume_name

        root_name = overlay_volume_name(system_id)
        pool = self._connection.storagePoolLookupByName(self._pool_name)
        volume = pool.storageVolLookupByName(root_name)
        path = volume.path()
        identity = self._identity.identity(path)
        if identity is None:
            raise ValueError("System root volume identity is unavailable")
        identity_bytes = json.dumps(
            {
                "kind": identity.kind,
                "primary": identity.primary,
                "secondary": identity.secondary,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        capacity = int(volume.info()[1])
        if capacity < 1:
            raise ValueError("System root volume capacity is invalid")
        return PreparedVolume(
            pool=self._pool_name,
            name=root_name,
            system_id=system_id,
            run_id=run_id,
            operation_nonce=operation_nonce,
            purpose="root",
            digest="sha256:"
            + hashlib.sha256(b"kdive-root-device-identity-v1\0" + identity_bytes).hexdigest(),
            capacity_bytes=capacity,
        )

    def configuration(self, architecture: str) -> RemoteModuleAuthorityHostConfiguration:
        appliance = load_installed_remote_module_appliance(self._appliance_root, architecture)
        emulator = {
            "x86_64": "/usr/bin/qemu-system-x86_64",
            "ppc64le": "/usr/bin/qemu-system-ppc64",
        }.get(architecture)
        if emulator is None:
            raise ValueError("unsupported remote module appliance architecture")

        def fixed_inspection_bypassed(
            identity_port: RemoteDeviceIdentityPort,
            present_attempt_volumes: frozenset[str] | None = None,
        ) -> AttachmentInspection:
            del identity_port, present_attempt_volumes
            raise AssertionError("fixed attachment inspection was bypassed")

        volumes = RemoteModuleVolumePreparation(
            storage=self._connection,
            pool_name=self._pool_name,
            entries=(),
            writer=Ext4SourceFilesystemWriter(self._work_dir),
            inspect_attachments=fixed_inspection_bypassed,
            work_dir=self._work_dir,
            artifact_stager=self._artifact_stager,
        )
        execution = RemoteModuleApplianceExecution(
            appliance=self._connection,
            architecture=architecture,
            emulator_path=emulator,
            memory_kib=262_144,
            vcpus=1,
            appliance_volume=None,
            appliance_image_digest=appliance.manifest_digest,
            root=lambda operation: self._root(
                operation.system_id, operation.run_id, operation.operation_nonce
            ),
            read_scratch_result=lambda volume, deadline: self._reader.read_volume(
                volume, deadline=deadline
            ),
            inspect_attachments=lambda: (_ for _ in ()).throw(
                AssertionError("fixed attachment inspection was bypassed")
            ),
            secret_registry=self._secret_registry,
            deadline_executor=self._deadline_executor,
            monotonic=self._monotonic,
            appliance_kernel=appliance.kernel,
            appliance_initrd=appliance.initrd,
            root_for_attempt=self._root,
        )
        return RemoteModuleAuthorityHostConfiguration(
            volumes=volumes,
            appliance=execution,
            identity=self._identity,
            fixed_inspection=True,
        )

    def host(self, architecture: str) -> ConcreteRemoteModuleAuthorityHost:
        return ConcreteRemoteModuleAuthorityHost(self.configuration(architecture), self._executor)


class FactoryRemoteModuleAuthorityHost:
    """Select only the request plan's architecture from one fixed production factory."""

    def __init__(self, factory: RemoteModuleAuthorityHostFactory) -> None:
        self._factory = factory

    async def derive_operation(
        self,
        authority: AuthorityPreparationMutationRequestV1,
        preparation: ModuleAttemptPreparationRequestV1,
    ) -> RemoteModuleOperationV1:
        return await self._factory.host(authority.plan.architecture).derive_operation(
            authority, preparation
        )

    async def execute(
        self, admitted: AdmittedRemoteModulePreparation
    ) -> RemoteModuleTerminalPreparationResponseV1:
        request = admitted.request
        return await self._factory.host(request.authority.plan.architecture).execute(admitted)
