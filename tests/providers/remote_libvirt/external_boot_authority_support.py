"""Remote external-boot authority fixtures shared by the provider and orphan-chain tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from kdive.providers.external_boot_authority.protocol import (
    AuthorityCommitContextV1,
    AuthorityMutationRequestV1,
    AuthorityOperation,
)
from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ExternalBootActivationBinding,
    ExternalBootPlan,
    OpaqueProviderRef,
    PresentComponentState,
    ProviderStateIdentity,
    RecoveryPoint,
)
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteExternalBootAuthorityAdapter,
    RemoteExternalBootCoordinator,
    RemoteExternalBootObservedState,
    RemoteExternalBootRecoveryRecord,
    RemoteModuleLifecycleResponseV1,
    RemoteModuleVolumePreparationRequestV1,
    RemoteModuleVolumePreparationStore,
)
from kdive.providers.remote_libvirt.lifecycle.external_boot import prepare_target_definition
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleRecoveryRefV2,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from tests.providers.remote_libvirt.authority_module_support import (
    _remote_preparation_request,
    _terminal_response,
)
from tests.providers.remote_libvirt.lifecycle.external_boot_support import (
    _materialization,
    _plan,
    _source_xml,
)


class _NativeOperations:
    """Remote operations fake recording mutations and serving one scripted live read."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.cleanup_owners: list[OpaqueProviderRef] = []
        self.acknowledged: list[tuple[str, str]] = []
        self.state = RemoteExternalBootObservedState(
            definition="target",
            preserved="sha256:" + "1" * 64,
            boot="sha256:" + "2" * 64,
            active=True,
            recovery_objects_absent=False,
        )
        self.fail_read = False

    def activate(self, recovery: object, owner: object, deadline: float) -> None:
        self.calls.append("activate")

    def recover(self, recovery: object, owner: object, deadline: float) -> None:
        self.calls.append("recover")

    def recover_acknowledged(
        self, recovery: object, owner: object, acknowledged: tuple[str, str], deadline: float
    ) -> None:
        self.calls.append("recover-acknowledged")
        self.acknowledged.append(acknowledged)

    def cleanup(
        self, recovery: RemoteExternalBootRecoveryRecord, owner: OpaqueProviderRef, deadline: float
    ) -> None:
        # The concrete cleanup's first act: the record must be re-presented under the
        # authority its module reference was minted for.
        recovery.module_recovery.validate_authority(owner)
        self.calls.append("cleanup")
        self.cleanup_owners.append(owner)

    def observe_state(self, recovery: object, deadline: float) -> RemoteExternalBootObservedState:
        if self.fail_read:
            raise RuntimeError("libvirt connection dropped")
        return self.state


def _native_adapter(
    tmp_path: Path,
    *,
    restored: bool = False,
    record: RemoteExternalBootRecoveryRecord | None = None,
) -> tuple[
    RemoteExternalBootAuthorityAdapter,
    _NativeOperations,
    RemoteExternalBootRecoveryRecord,
    RecoveryPoint,
    OpaqueProviderRef,
]:
    """A real store holding one prepared remote activation, behind the native adapter."""
    record = record or _record()
    store = RemoteModuleVolumePreparationStore(tmp_path)
    _publish_terminal(store, record)
    prepared = store.reopen_terminal(record.binding, record.plan_identity).request.authority
    preparation_authority = OpaqueProviderRef(
        ref=f"authority/{prepared.authority_id}/{prepared.generation}/{prepared.attempt_id}"
    )
    record = record.model_copy(
        update={
            "module_recovery": record.module_recovery.model_copy(
                update={
                    "authority_identity": RemoteModuleRecoveryRefV2.identity_for_authority(
                        preparation_authority
                    )
                }
            )
        }
    )
    store.publish_recovery(record)
    if restored:
        key = store._terminal_key(
            record.binding.system_id,
            record.binding.activation_id,
            record.binding.run_id,
            record.plan_identity,
        )
        store._publish(
            f"{key}.restored",
            _restored_response(
                store.reopen_terminal(record.binding, record.plan_identity).request
            ).to_canonical_json(),
        )
    point = store.recovery_point(record.binding, record.plan_identity)
    operations = _NativeOperations()
    adapter = RemoteExternalBootAuthorityAdapter(
        RemoteExternalBootCoordinator(cast(Any, operations), store, lambda: 300.0),
        RemoteModulePreparationExecutor(),
        close=store.close,
    )
    return adapter, operations, record, point, preparation_authority


def _native_request(
    record: RemoteExternalBootRecoveryRecord,
    point: RecoveryPoint,
    *,
    purpose: str,
    operation: AuthorityOperation,
    named: bool = False,
    generation: int = 9,
) -> AuthorityMutationRequestV1:
    return AuthorityMutationRequestV1.model_validate(
        {
            "authority_id": uuid4(),
            "generation": generation,
            "system_id": record.binding.system_id,
            "activation_id": record.binding.activation_id,
            "run_id": record.binding.run_id,
            "plan_identity": record.plan_identity,
            "purpose": purpose,
            "operation": operation,
            "provider_kind": "remote-libvirt",
            "authority_instance": "remote-a",
            "operation_identity": f"{purpose}-{operation.value}",
            "operation_digest": "sha256:" + "c" * 64,
            "attempt_id": uuid4(),
            "expected_source_identity": record.source_state.definition,
            "intended_target_identity": record.target_state.definition,
            "recovery_objects": (
                [
                    {
                        "system_id": record.binding.system_id,
                        "activation_id": record.binding.activation_id,
                        "reference": point.recovery_ref.ref,
                    }
                ]
                if named
                else []
            ),
        }
    )


def _native_context(request: AuthorityMutationRequestV1) -> AuthorityCommitContextV1:
    return AuthorityCommitContextV1(
        commit_point=request.operation,
        operation_identity=request.operation_identity,
        attempt_id=request.attempt_id,
        journal_sequence=3,
        journal_digest="sha256:" + "a" * 64,
    )


def _restored_response(
    request: RemoteModuleVolumePreparationRequestV1,
) -> RemoteModuleLifecycleResponseV1:
    terminal = _terminal_response(request)
    capture = request.operation
    operation = RemoteModuleOperationV1(
        operation="restore",
        system_id=capture.system_id,
        run_id=capture.run_id,
        plan_identity=capture.plan_identity,
        operation_nonce=capture.operation_nonce,
        release=capture.release,
        root_volume=capture.root_volume,
        source_manifest=capture.source_manifest,
        capture_absent=True,
        installed_manifest=capture.source_manifest,
        appliance_image_digest=capture.appliance_image_digest,
    )
    result = terminal.result.model_copy(
        update={"phase": "restored", "entry_count": None, "content_bytes": None}
    )
    return RemoteModuleLifecycleResponseV1(
        action="restore",
        recovery=terminal.recovery,
        operation=operation,
        result=result,
        volumes_absent=False,
    )


def _record(
    *,
    system_id: str = "00000000-0000-4000-8000-000000000001",
    run_id: str = "00000000-0000-4000-8000-000000000002",
    activation_id: str = "00000000-0000-4000-8000-000000000003",
) -> RemoteExternalBootRecoveryRecord:
    base_plan = _plan()
    plan = base_plan.model_copy(
        update={
            "ownership": base_plan.ownership.model_copy(
                update={"system_id": system_id, "run_id": run_id}
            )
        }
    )
    materialization = _materialization(plan=plan)
    materialization = materialization.model_copy(
        update={
            "ownership": materialization.ownership.model_copy(
                update={"system_id": system_id, "run_id": run_id}
            )
        }
    )
    binding = ExternalBootActivationBinding(
        system_id=plan.ownership.system_id,
        run_id=plan.ownership.run_id,
        activation_id=activation_id,
    )
    definition = prepare_target_definition(
        _source_xml(system_id=UUID(system_id)),
        plan=plan,
        materialization=materialization,
        binding=binding,
        pool="kdive",
        overlay_path="/pool/overlay.qcow2",
        kernel_path="/artifacts/kernel",
        initrd_path="/artifacts/initrd",
    )
    digest = "sha256:" + "a" * 64
    module = RemoteModuleRecoveryRefV2.model_validate(
        {
            "system_id": binding.system_id,
            "run_id": binding.run_id,
            "plan_identity": plan.identity,
            "operation_nonce": "b" * 32,
            "pool": {"ref": "pool/modules"},
            "root_volume": {"ref": "volumes/root"},
            "source_volume": {"ref": "volumes/source"},
            "scratch_volume": {"ref": "volumes/scratch"},
            "operation_identity": digest,
            "result_identity": digest,
            "installed_entry_count": 1,
            "installed_content_bytes": 3,
            "appliance_image_digest": digest,
            "authority_identity": digest,
            "source_capacity_bytes": 4096,
        }
    )
    return RemoteExternalBootRecoveryRecord(
        binding=binding,
        plan_identity=plan.identity,
        materialization=materialization,
        definition=definition,
        module_recovery=module,
        source_state=ProviderStateIdentity(
            definition=definition.source_definition, modules=AbsentComponentState()
        ),
        target_state=ProviderStateIdentity(
            definition=definition.target_definition,
            modules=PresentComponentState(manifest=materialization.installed_module_tree),
        ),
        prior_power="inactive",
        recovery_objects=tuple(
            sorted(
                (
                    OpaqueProviderRef(ref="volumes/source"),
                    OpaqueProviderRef(ref="volumes/scratch"),
                ),
                key=lambda value: value.to_canonical_json(),
            )
        ),
    )


def _plan_for_record(record: RemoteExternalBootRecoveryRecord) -> ExternalBootPlan:
    base = _plan()
    return base.model_copy(
        update={
            "ownership": base.ownership.model_copy(
                update={
                    "system_id": record.binding.system_id,
                    "run_id": record.binding.run_id,
                }
            )
        }
    )


def _publish_terminal(
    store: RemoteModuleVolumePreparationStore, record: RemoteExternalBootRecoveryRecord
) -> None:
    plan = _plan_for_record(record)
    base = _remote_preparation_request()
    authority = base.authority.model_copy(
        update={
            "system_id": UUID(record.binding.system_id),
            "activation_id": UUID(record.binding.activation_id),
            "run_id": UUID(record.binding.run_id),
            "plan_identity": plan.identity,
            "plan": plan,
        }
    )
    operation = base.operation.model_copy(
        update={
            "system_id": record.binding.system_id,
            "run_id": record.binding.run_id,
            "plan_identity": plan.identity,
            "release": plan.module_obligation.release,
            "source_manifest": plan.module_obligation.source_manifest,
        }
    )
    request = RemoteModuleVolumePreparationRequestV1(authority=authority, operation=operation)
    store.stage(request, 999.0)
    store.publish_result(request, _terminal_response(request))
