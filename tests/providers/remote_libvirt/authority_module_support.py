"""Canonical remote authority module request and terminal evidence fixtures."""

from uuid import UUID, uuid4

from kdive.providers.external_boot_authority.protocol import AuthorityPreparationMutationRequestV1
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteModuleTerminalPreparationResponseV1,
    RemoteModuleVolumePreparationRequestV1,
    RemoteModuleVolumePreparationResponseV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleRecoveryRefV2,
    RemoteModuleResultV1,
    identity_for,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volume_names import (
    render_module_volume_name,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    PreparedModuleVolumes,
    PreparedVolume,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance_support import (
    operation as module_operation,
)
from tests.support.external_boot_plan import external_boot_plan


def _remote_preparation_request() -> RemoteModuleVolumePreparationRequestV1:
    operation = module_operation()
    system_id = UUID(operation.system_id)
    run_id = UUID(operation.run_id)
    plan = external_boot_plan(system_id, run_id)
    operation = operation.model_copy(
        update={
            "plan_identity": plan.identity,
            "release": plan.module_obligation.release,
            "source_manifest": plan.module_obligation.source_manifest,
        }
    )
    authority = AuthorityPreparationMutationRequestV1(
        authority_id=uuid4(),
        generation=1,
        system_id=system_id,
        activation_id=uuid4(),
        run_id=run_id,
        plan_identity=plan.identity,
        purpose="activate",
        operation="prepare",
        provider_kind="remote-libvirt",
        authority_instance="remote-a",
        operation_identity="prepare-op",
        operation_digest="sha256:" + "c" * 64,
        attempt_id=uuid4(),
        expected_source_identity="source-a",
        intended_target_identity="target-a",
        recovery_objects=(),
        plan=plan,
    )
    return RemoteModuleVolumePreparationRequestV1(authority=authority, operation=operation)


def _prepared_volumes(request: RemoteModuleVolumePreparationRequestV1) -> PreparedModuleVolumes:
    common = {
        "pool": "modules",
        "system_id": request.operation.system_id,
        "run_id": request.operation.run_id,
        "operation_nonce": request.operation.operation_nonce,
    }
    return PreparedModuleVolumes(
        source=PreparedVolume(
            **common,
            name=render_module_volume_name(
                request.operation.system_id,
                request.operation.run_id,
                request.operation.operation_nonce,
                "source.ext4",
            ),
            purpose="source",
            digest=request.operation.source_manifest,
            capacity_bytes=4096,
        ),
        scratch=PreparedVolume(
            **common,
            name=render_module_volume_name(
                request.operation.system_id,
                request.operation.run_id,
                request.operation.operation_nonce,
                "scratch.ext4",
            ),
            purpose="scratch",
            digest="sha256:" + "0" * 64,
            capacity_bytes=8192,
        ),
    )


def _terminal_response(
    request: RemoteModuleVolumePreparationRequestV1,
) -> RemoteModuleTerminalPreparationResponseV1:
    operation = request.operation
    volumes = _prepared_volumes(request)
    result = RemoteModuleResultV1(
        status="success",
        phase="installed",
        system_id=operation.system_id,
        run_id=operation.run_id,
        plan_identity=operation.plan_identity,
        operation_nonce=operation.operation_nonce,
        appliance_image_digest=operation.appliance_image_digest,
        release=operation.release,
        root_volume_key=operation.root_volume.key,
        root_volume_identity=operation.root_volume.identity,
        source_manifest=operation.source_manifest,
        installed_manifest=operation.source_manifest,
        capture_absent=True,
        entry_count=1,
        content_bytes=3,
    )
    authority = request.authority
    authority_ref = OpaqueProviderRef(
        ref=f"authority/{authority.authority_id}/{authority.generation}/{authority.attempt_id}"
    )
    base = RemoteModuleVolumePreparationResponseV1.from_prepared(volumes)
    return RemoteModuleTerminalPreparationResponseV1(
        source=base.source,
        scratch=base.scratch,
        result=result,
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
            result_identity=identity_for(result),
            installed_entry_count=1,
            installed_content_bytes=3,
            appliance_image_digest=operation.appliance_image_digest,
            authority_identity=RemoteModuleRecoveryRefV2.identity_for_authority(authority_ref),
        ),
    )
