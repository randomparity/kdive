"""Closed remote external-boot coordinator contracts (#2200)."""

from __future__ import annotations

import inspect
from uuid import UUID

import pytest
from pydantic import ValidationError

from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ExternalBootActivationBinding,
    OpaqueProviderRef,
    PresentComponentState,
    ProviderStateIdentity,
)
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteExternalBootOperations,
    RemoteExternalBootRecoveryRecord,
)
from kdive.providers.remote_libvirt.lifecycle.external_boot import prepare_target_definition
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleRecoveryRefV2,
)
from tests.providers.remote_libvirt.lifecycle.test_external_boot import (
    _materialization,
    _plan,
    _source_xml,
)


def _record() -> RemoteExternalBootRecoveryRecord:
    system_id = "00000000-0000-4000-8000-000000000001"
    run_id = "00000000-0000-4000-8000-000000000002"
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
        activation_id="00000000-0000-4000-8000-000000000003",
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


def test_recovery_record_round_trips_only_canonical_closed_bytes() -> None:
    record = _record()
    encoded = record.to_canonical_json()

    assert RemoteExternalBootRecoveryRecord.from_canonical_json(encoded) == record
    with pytest.raises(ValueError, match="not canonical"):
        RemoteExternalBootRecoveryRecord.from_canonical_json(b" " + encoded)
    with pytest.raises(ValidationError, match="Extra inputs"):
        RemoteExternalBootRecoveryRecord.model_validate(
            {**record.model_dump(mode="json", by_alias=True), "destination": "host"}
        )


@pytest.mark.parametrize("mismatch", ["system", "plan", "definition", "modules"], ids=str)
def test_recovery_record_rejects_cross_owned_or_mismatched_facts(mismatch: str) -> None:
    record = _record()
    changes: dict[str, object]
    if mismatch == "system":
        changes = {
            "module_recovery": record.module_recovery.model_copy(
                update={"system_id": "00000000-0000-4000-8000-000000000099"}
            )
        }
    elif mismatch == "plan":
        changes = {"plan_identity": "sha256:" + "f" * 64}
    elif mismatch == "definition":
        changes = {
            "target_state": record.target_state.model_copy(
                update={"definition": "sha256:" + "f" * 64}
            )
        }
    else:
        changes = {
            "target_state": record.target_state.model_copy(
                update={"modules": PresentComponentState(manifest="sha256:" + "f" * 64)}
            )
        }
    with pytest.raises(ValidationError, match="remote recovery"):
        RemoteExternalBootRecoveryRecord.model_validate(
            {**record.model_dump(mode="json", by_alias=True), **changes}
        )


def test_recovery_objects_are_sorted_unique_and_bounded() -> None:
    record = _record()
    for objects in (
        tuple(reversed(record.recovery_objects)),
        (record.recovery_objects[0], record.recovery_objects[0]),
    ):
        with pytest.raises(ValidationError, match="unique and canonically sorted"):
            RemoteExternalBootRecoveryRecord.model_validate(
                {**record.model_dump(mode="json", by_alias=True), "recovery_objects": objects}
            )


def test_operations_protocol_has_exact_six_deadline_bearing_methods() -> None:
    methods = {
        name: value
        for name, value in vars(RemoteExternalBootOperations).items()
        if callable(value) and not name.startswith("_")
    }
    assert set(methods) == {"materialize", "prepare", "activate", "observe", "recover", "cleanup"}
    for method in methods.values():
        parameters = inspect.signature(method).parameters
        assert list(parameters)[-1] == "deadline"
        assert parameters["deadline"].annotation in {"float", float}
