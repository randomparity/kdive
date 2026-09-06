"""Concrete provider-host remote-module preparation under one completion owner."""

from __future__ import annotations

from dataclasses import dataclass

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.providers.remote_libvirt.external_boot_authority import (
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
    RemoteDeviceIdentityPort,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleRecoveryRefV2,
    RemoteModuleResultV1,
    identity_for,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_operation import (
    RemoteModuleApplianceExecution,
    RemoteModuleVolumePreparation,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    PreparedModuleVolumes,
    VolumeRequest,
    prepare_attempt_volumes,
)


@dataclass(frozen=True, slots=True)
class RemoteModuleAuthorityHostConfiguration:
    """Fixed provider-host resources; no caller-selected connection, path, or executable."""

    volumes: RemoteModuleVolumePreparation
    appliance: RemoteModuleApplianceExecution
    identity: RemoteDeviceIdentityPort


class ConcreteRemoteModuleAuthorityHost:
    """Create both volumes, run/adopt the appliance, reopen its result, and tear it down."""

    def __init__(
        self,
        configuration: RemoteModuleAuthorityHostConfiguration,
        executor: RemoteModulePreparationExecutor,
    ) -> None:
        self._configuration = configuration
        self._executor = executor

    def _volume_request(self, request: RemoteModuleVolumePreparationRequestV1) -> VolumeRequest:
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
            inspect_attachments=lambda: configured.inspect_attachments(
                self._configuration.identity
            ),
            work_dir=configured.work_dir,
        )

    def _appliance_request(
        self,
        request: RemoteModuleVolumePreparationRequestV1,
        volumes: PreparedModuleVolumes,
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
            appliance_image_digest=configured.appliance_image_digest,
            root=configured.root(operation),
            source=volumes.source,
            scratch=volumes.scratch,
            operation=operation,
            secret_registry=configured.secret_registry,
            read_scratch_result=lambda: configured.read_scratch_result(
                volumes.scratch, request.deadline
            ),
            inspect_attachments=configured.inspect_attachments,
            executor=configured.deadline_executor,
            monotonic=configured.monotonic,
            invocation_deadline=request.deadline,
        )

    def _execute(
        self, request: RemoteModuleVolumePreparationRequestV1
    ) -> RemoteModuleTerminalPreparationResponseV1:
        operation = request.operation
        volumes = prepare_attempt_volumes(
            self._configuration.volumes.storage,
            self._volume_request(request),
            admit_mutation=lambda: self._require_deadline(request.deadline),
        )
        appliance_request = self._appliance_request(request, volumes)
        outcome = run_or_adopt_appliance(self._configuration.appliance.appliance, appliance_request)
        if outcome.result is None:
            raise CategorizedError(
                "remote module appliance did not produce a durable result",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"timed_out": outcome.timed_out},
            )
        raw = self._configuration.appliance.read_scratch_result(volumes.scratch, request.deadline)
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
        self, request: RemoteModuleVolumePreparationRequestV1
    ) -> RemoteModuleTerminalPreparationResponseV1:
        return await self._executor.run(lambda: self._execute(request))
