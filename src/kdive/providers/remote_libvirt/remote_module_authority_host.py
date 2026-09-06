"""Concrete provider-host remote-module preparation under one completion owner."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.external_boot_authority.protocol import (
    AuthorityOperation,
    AuthorityPreparationMutationRequestV1,
)
from kdive.providers.ports.external_boot import ExternalBootArtifactStager, OpaqueProviderRef
from kdive.providers.remote_libvirt.external_boot_authority import (
    AdmittedRemoteModulePreparation,
    RemoteModuleLifecycleRequestV1,
    RemoteModuleLifecycleResponseV1,
    RemoteModuleSystemTeardownRequestV1,
    RemoteModuleTerminalPreparationResponseV1,
    RemoteModuleTerminalRecord,
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
    delete_owned_attempt_volume,
    prepare_attempt_volumes,
    recovery_attempt_volumes,
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
        *,
        cleanup: bool = False,
    ) -> AttachmentInspection:
        if not self._configuration.fixed_inspection:
            return self._configuration.volumes.inspect_attachments(
                self._configuration.identity, present_attempt_volumes
            )
        return inspect_module_attachments(
            cast(AttachmentConn, self._configuration.volumes.storage),
            self._configuration.identity,
            self._expected_attachments(operation),
            present_attempt_volumes,
            allow_cleanup_partial=cleanup,
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
        self,
        authority: AuthorityPreparationMutationRequestV1,
        operation: RemoteModuleOperationV1,
        archive: Path,
    ) -> VolumeRequest:
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
        operation: RemoteModuleOperationV1,
        volumes: PreparedModuleVolumes,
        root: PreparedVolume,
        deadline: float,
    ) -> ApplianceRequest:
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
                self._volume_request(request.authority, operation, directory / "modules"),
                admit_mutation=lambda: self._require_deadline(deadline),
            )
        appliance_request = self._appliance_request(operation, volumes, root, deadline)
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

    @staticmethod
    def _restore_operation(
        capture: RemoteModuleOperationV1, installed: RemoteModuleResultV1
    ) -> RemoteModuleOperationV1:
        installed.validate_for(capture)
        if installed.phase != "installed" or installed.capture_state is None:
            raise ValueError("remote module installed baseline is incomplete")
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

    def _lifecycle_baseline(
        self,
        request: RemoteModuleLifecycleRequestV1 | RemoteModuleSystemTeardownRequestV1,
        terminal: RemoteModuleTerminalRecord,
    ) -> tuple[
        RemoteModuleOperationV1,
        RemoteModuleOperationV1,
        RemoteModuleResultV1,
        RemoteModuleRecoveryRefV2,
    ]:
        recovery = terminal.response.recovery
        authority = request.authority
        if (
            recovery.system_id != str(authority.system_id)
            or recovery.run_id != str(authority.run_id)
            or recovery.plan_identity != authority.plan_identity
        ):
            raise ValueError("remote module lifecycle authority differs from preparation")
        capture = terminal.request.operation
        installed = terminal.response.result
        if (
            identity_for(capture) != recovery.operation_identity
            or identity_for(installed) != recovery.result_identity
        ):
            raise ValueError("remote module lifecycle baseline identity differs")
        return capture, self._restore_operation(capture, installed), installed, recovery

    def _restore(
        self,
        request: RemoteModuleLifecycleRequestV1,
        terminal: RemoteModuleTerminalRecord,
        deadline: float,
    ) -> RemoteModuleLifecycleResponseV1:
        _capture, restore, _installed, recovery = self._lifecycle_baseline(request, terminal)
        configured = self._configuration.volumes
        stager = configured.artifact_stager
        if stager is None:
            raise ValueError("remote module artifact staging is not configured")
        with tempfile.TemporaryDirectory(
            prefix="kdive-remote-module-restore-", dir=configured.work_dir
        ) as raw:
            directory = Path(raw)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                evidence, _manifest = stager.materialize_artifacts(
                    terminal.request.authority.plan, descriptor
                )
            finally:
                os.close(descriptor)
            if evidence.get("module_source_manifest") != restore.source_manifest:
                raise ValueError("remote module restore source differs from baseline")
            volumes = prepare_attempt_volumes(
                configured.storage,
                self._volume_request(terminal.request.authority, restore, directory / "modules"),
                admit_mutation=lambda: self._require_deadline(deadline),
            )
        root = self._configuration.appliance.root(restore)
        appliance_request = self._appliance_request(restore, volumes, root, deadline)
        outcome = run_or_adopt_appliance(self._configuration.appliance.appliance, appliance_request)
        if outcome.result is None:
            raise RuntimeError("remote module restore did not produce durable terminal evidence")
        result = outcome.result
        result.validate_for(restore)
        if result.status != "success" or result.phase != "restored":
            raise ValueError("remote module restore did not reach restored success")
        teardown = teardown_remote_module_appliance(
            self._configuration.appliance.appliance, appliance_request
        )
        if not teardown.complete:
            raise RuntimeError("remote module restore teardown is incomplete")
        return RemoteModuleLifecycleResponseV1(
            action="restore",
            recovery=recovery,
            operation=restore,
            result=result,
            volumes_absent=False,
        )

    def _reap(
        self,
        request: RemoteModuleLifecycleRequestV1 | RemoteModuleSystemTeardownRequestV1,
        terminal: RemoteModuleTerminalRecord,
        restored: RemoteModuleLifecycleResponseV1 | None,
        deadline: float,
    ) -> RemoteModuleLifecycleResponseV1:
        capture, restore, installed, recovery = self._lifecycle_baseline(request, terminal)
        if restored is None and request.authority.operation is not AuthorityOperation.TEARDOWN:
            raise ValueError("installed-only module reap requires teardown authority")
        if restored is not None and (
            restored.operation != restore or restored.recovery != recovery
        ):
            raise ValueError("remote module restored evidence differs from preparation")
        volumes = recovery_attempt_volumes(
            restore, recovery.pool.ref, recovery.source_capacity_bytes
        )
        if (
            recovery.source_volume.ref != volumes.source.name
            or recovery.scratch_volume.ref != volumes.scratch.name
            or recovery.root_volume.ref != restore.root_volume.key
            or recovery.pool.ref != self._configuration.volumes.pool_name
        ):
            raise ValueError("remote module recovery geometry differs from fixed host binding")
        operation = restore if restored is not None else capture
        result = restored.result if restored is not None else installed
        state = self._reap_state(recovery)
        if state == "reaped":
            self._require_attempt_volumes_absent(volumes)
            return RemoteModuleLifecycleResponseV1(
                action="reap",
                recovery=recovery,
                operation=operation,
                result=result,
                volumes_absent=True,
            )
        if state == "absent":
            raw = self._configuration.appliance.read_scratch_result(volumes.scratch, deadline)
            if raw is None or RemoteModuleResultV1.from_wire_bytes(raw) != result:
                raise ValueError("remote module restored result differs before reap")
        root = self._configuration.appliance.root(restore)
        appliance_request = self._appliance_request(restore, volumes, root, deadline)
        teardown = teardown_remote_module_appliance(
            self._configuration.appliance.appliance, appliance_request
        )
        if not teardown.complete:
            raise RuntimeError("remote module reap teardown is incomplete")
        if state == "absent":
            self._record_reap_marker(recovery, "reaping", deadline)
        present = self._present_attempt_volumes(volumes)
        inspection = self._inspect(restore, present, cleanup=True)
        delete_owned_attempt_volume(
            self._configuration.volumes.storage,
            volumes.source,
            inspection=inspection,
            admit_mutation=lambda: self._require_deadline(deadline),
        )
        present = self._present_attempt_volumes(volumes)
        inspection = self._inspect(restore, present, cleanup=True)
        delete_owned_attempt_volume(
            self._configuration.volumes.storage,
            volumes.scratch,
            inspection=inspection,
            admit_mutation=lambda: self._require_deadline(deadline),
        )
        self._record_reap_marker(recovery, "reaped", deadline)
        return RemoteModuleLifecycleResponseV1(
            action="reap",
            recovery=recovery,
            operation=operation,
            result=result,
            volumes_absent=True,
        )

    def _marker_name(self, recovery: RemoteModuleRecoveryRefV2, state: str) -> str:
        return render_module_volume_name(
            recovery.system_id,
            recovery.run_id,
            recovery.operation_nonce,
            f"{state}.journal",
        )

    def _marker_present(self, recovery: RemoteModuleRecoveryRefV2, state: str) -> bool:
        pool = self._configuration.volumes.storage.storagePoolLookupByName(recovery.pool.ref)
        try:
            pool.storageVolLookupByName(self._marker_name(recovery, state))
            return True
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                return False
            raise

    def _reap_state(self, recovery: RemoteModuleRecoveryRefV2) -> str:
        reaping = self._marker_present(recovery, "reaping")
        reaped = self._marker_present(recovery, "reaped")
        if reaped and not reaping:
            raise ValueError("remote module reap markers are out of order")
        if reaped:
            return "reaped"
        return "reaping" if reaping else "absent"

    def _record_reap_marker(
        self, recovery: RemoteModuleRecoveryRefV2, state: str, deadline: float
    ) -> None:
        if self._marker_present(recovery, state):
            return
        self._require_deadline(deadline)
        root = ET.Element("volume")
        ET.SubElement(root, "name").text = self._marker_name(recovery, state)
        ET.SubElement(root, "capacity", unit="bytes").text = "1"
        target = ET.SubElement(root, "target")
        ET.SubElement(target, "format", type="raw")
        pool = self._configuration.volumes.storage.storagePoolLookupByName(recovery.pool.ref)
        pool.createXML(ET.tostring(root, encoding="unicode"), 0)
        if not self._marker_present(recovery, state):
            raise RuntimeError("remote module reap marker was not durable after creation")

    def _present_attempt_volumes(self, volumes: PreparedModuleVolumes) -> frozenset[str]:
        pool = self._configuration.volumes.storage.storagePoolLookupByName(volumes.source.pool)
        present: set[str] = set()
        for volume in (volumes.source, volumes.scratch):
            try:
                pool.storageVolLookupByName(volume.name)
                present.add(volume.name)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() != libvirt.VIR_ERR_NO_STORAGE_VOL:
                    raise
        return frozenset(present)

    def _require_attempt_volumes_absent(self, volumes: PreparedModuleVolumes) -> None:
        if self._present_attempt_volumes(volumes):
            raise ValueError("remote module reaped marker conflicts with present attempt volumes")

    async def execute_lifecycle(
        self,
        request: RemoteModuleLifecycleRequestV1,
        terminal: RemoteModuleTerminalRecord,
        restored: RemoteModuleLifecycleResponseV1 | None,
        deadline: float,
    ) -> RemoteModuleLifecycleResponseV1:
        if request.action == "restore":
            return await self._executor.run(lambda: self._restore(request, terminal, deadline))
        return await self._executor.run(lambda: self._reap(request, terminal, restored, deadline))

    async def execute_system_teardown(
        self,
        request: RemoteModuleSystemTeardownRequestV1,
        terminal: RemoteModuleTerminalRecord,
        deadline: float,
    ) -> RemoteModuleLifecycleResponseV1:
        return await self._executor.run(lambda: self._reap(request, terminal, None, deadline))


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

    async def execute_lifecycle(
        self,
        request: RemoteModuleLifecycleRequestV1,
        terminal: RemoteModuleTerminalRecord,
        restored: RemoteModuleLifecycleResponseV1 | None,
        deadline: float,
    ) -> RemoteModuleLifecycleResponseV1:
        architecture = terminal.request.authority.plan.architecture
        return await self._factory.host(architecture).execute_lifecycle(
            request, terminal, restored, deadline
        )

    async def execute_system_teardown(
        self,
        request: RemoteModuleSystemTeardownRequestV1,
        terminal: RemoteModuleTerminalRecord,
        deadline: float,
    ) -> RemoteModuleLifecycleResponseV1:
        architecture = terminal.request.authority.plan.architecture
        return await self._factory.host(architecture).execute_system_teardown(
            request, terminal, deadline
        )
