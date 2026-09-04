"""Provider-neutral external-boot contract tests (ADR-0583)."""

from __future__ import annotations

import json
from typing import cast

import pytest
from pydantic import ValidationError

from kdive.providers.fault_inject.lifecycle.external_boot import FaultInjectExternalBoot
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    ExternalBootPreparationObservation,
    ExternalBootPreparationRequest,
    OpaqueProviderRef,
    RecoveryPoint,
    RootSpecV1,
    RunningKernelObservation,
)

ZERO_DIGEST = "sha256:" + "0" * 64
SYSTEM_ID = "00000000-0000-0000-0000-000000000003"
RUN_ID = "00000000-0000-0000-0000-000000000002"
BUILD_GENERATION = "00000000-0000-0000-0000-000000000001"
ACTIVATION_ID = "00000000-0000-0000-0000-000000000004"


def _plan_data() -> dict[str, object]:
    return {
        "architecture": "x86_64",
        "bundle": {
            "decoded_kernel_size_bytes": 200,
            "elf_metadata_bytes": 50,
            "gnu_build_id_size_bytes": 20,
            "key": "bundles/k.tar",
            "member_count": 2,
            "sha256": ZERO_DIGEST,
            "uncompressed_bytes": 101,
            "version": "v1",
            "vmlinuz_sha256": ZERO_DIGEST,
            "vmlinuz_size_bytes": 100,
        },
        "cmdline": "root=UUID=x",
        "debug_cmdline": None,
        "initrd": None,
        "module_obligation": {
            "member_count": 1,
            "mode": "system-root-tree",
            "release": "6.1.0",
            "source_manifest": ZERO_DIGEST,
            "uncompressed_bytes": 1,
        },
        "ownership": {
            "build_generation": BUILD_GENERATION,
            "run_id": RUN_ID,
            "system_id": SYSTEM_ID,
        },
        "platform_arguments": ["root=UUID=x"],
        "root": {
            "architecture": "x86_64",
            "arguments": ["root=UUID=x"],
            "authority": "stage-inspection",
            "root": "UUID=x",
            "schema": "root-spec-v1",
            "source": {"identity": ZERO_DIGEST, "kind": "staged-image"},
        },
        "schema": "external-boot-plan-v1",
    }


def test_plan_matches_adr_golden_vector_and_identity() -> None:
    plan = ExternalBootPlan.model_validate(_plan_data())

    assert (
        plan.to_canonical_json()
        == json.dumps(_plan_data(), sort_keys=True, separators=(",", ":")).encode()
    )
    assert plan.identity == (
        "sha256:a526825f6daf93774d3892c515332ce86390d914c1ff8faf1d994f24a9ea061b"
    )
    assert ExternalBootPlan.from_canonical_json(plan.to_canonical_json()) == plan


def test_materialization_matches_adr_golden_vector_and_identity() -> None:
    data = {
        "architecture": "x86_64",
        "artifacts": {
            "initrd": None,
            "kernel": {"ref": "kernel/ref"},
            "modules": {"ref": "modules/ref"},
        },
        "extracted_vmlinuz_sha256": ZERO_DIGEST,
        "installed_module_tree": ZERO_DIGEST,
        "kernel_observation": {
            "architecture": "x86_64",
            "gnu_build_id": "0" * 40,
            "release": "6.1.0",
        },
        "ownership": {"run_id": RUN_ID, "system_id": SYSTEM_ID},
        "plan_identity": "sha256:a526825f6daf93774d3892c515332ce86390d914c1ff8faf1d994f24a9ea061b",
        "provider_kind": "local-libvirt",
        "schema": "external-boot-materialization-v1",
        "source_module_manifest": ZERO_DIGEST,
        "verified_bundle_sha256": ZERO_DIGEST,
        "verified_initrd_sha256": None,
    }
    materialization = ExternalBootMaterialization.model_validate(data)

    assert materialization.identity == (
        "sha256:dc2cdf6635a5caca475257f6c62c886cdd763e1858c5fb63a95346d800b54361"
    )
    assert (
        ExternalBootMaterialization.from_canonical_json(materialization.to_canonical_json())
        == materialization
    )


def test_non_null_initrd_matches_adr_boundary_vectors() -> None:
    plan_data = _plan_data()
    plan_data["initrd"] = {
        "key": "initrd/i.img",
        "sha256": ZERO_DIGEST,
        "size_bytes": 536_870_912,
        "version": "v1",
    }
    plan = ExternalBootPlan.model_validate(plan_data)
    assert plan.identity == (
        "sha256:3727eedd7d5a4b3740828f083229b7aa67ebca0497b959dcf9727a64ced6e488"
    )

    materialization_data = {
        "architecture": "x86_64",
        "artifacts": {
            "initrd": {"ref": "initrd/ref"},
            "kernel": {"ref": "kernel/ref"},
            "modules": {"ref": "modules/ref"},
        },
        "extracted_vmlinuz_sha256": ZERO_DIGEST,
        "installed_module_tree": ZERO_DIGEST,
        "kernel_observation": {
            "architecture": "x86_64",
            "gnu_build_id": "0" * 40,
            "release": "6.1.0",
        },
        "ownership": {"run_id": RUN_ID, "system_id": SYSTEM_ID},
        "plan_identity": plan.identity,
        "provider_kind": "local-libvirt",
        "schema": "external-boot-materialization-v1",
        "source_module_manifest": ZERO_DIGEST,
        "verified_bundle_sha256": ZERO_DIGEST,
        "verified_initrd_sha256": ZERO_DIGEST,
    }
    materialization = ExternalBootMaterialization.model_validate(materialization_data)
    assert materialization.identity == (
        "sha256:c1bcec0d307105434d087774bc6a3fa61ce9be5250fcdff0d2dfb6a8ae152aab"
    )
    assert ExternalBootPlan.from_canonical_json(plan.to_canonical_json()) == plan
    assert (
        ExternalBootMaterialization.from_canonical_json(materialization.to_canonical_json())
        == materialization
    )


def test_plan_rejects_initrd_larger_than_v1_limit() -> None:
    data = _plan_data()
    data["initrd"] = {
        "key": "initrd/i.img",
        "sha256": ZERO_DIGEST,
        "size_bytes": 536_870_913,
        "version": "v1",
    }

    with pytest.raises(ValidationError, match="less than or equal to 536870912"):
        ExternalBootPlan.model_validate(data)


def test_internal_schema_field_name_is_not_a_wire_alias() -> None:
    data = _plan_data()
    data["schema_"] = data.pop("schema")

    with pytest.raises(ValidationError):
        ExternalBootPlan.model_validate(data)


@pytest.mark.parametrize(
    ("authority", "kind"),
    [
        ("stage-inspection", "catalog-image"),
        ("catalog-attestation", "staged-image"),
    ],
)
def test_root_rejects_invalid_authority_source_pair(authority: str, kind: str) -> None:
    data = cast(dict[str, object], _plan_data()["root"])
    data = {**data, "authority": authority, "source": {"kind": kind, "identity": ZERO_DIGEST}}

    with pytest.raises(ValidationError, match="authority/source"):
        RootSpecV1.model_validate(data)


@pytest.mark.parametrize(
    "mutation",
    [
        {"unknown": True},
        {
            "ownership": {
                "build_generation": BUILD_GENERATION,
                "run_id": "abcdefab-cdef-abcd-efab-cdefabcdefab".upper(),
                "system_id": SYSTEM_ID,
            }
        },
        {"platform_arguments": ["root=UUID=x", "root=/dev/evil"]},
        {"platform_arguments": ["root=UUID=x"], "cmdline": "root=UUID=y"},
    ],
)
def test_plan_rejects_unknown_noncanonical_or_inconsistent_values(
    mutation: dict[str, object],
) -> None:
    data = {**_plan_data(), **mutation}

    with pytest.raises(ValidationError):
        ExternalBootPlan.model_validate(data)


@pytest.mark.parametrize("argument", ["=bad", "="])
def test_plan_rejects_empty_platform_argument_keys(argument: str) -> None:
    data = _plan_data()
    data["platform_arguments"] = ["root=UUID=x", argument]
    data["cmdline"] = f"root=UUID=x {argument}"

    with pytest.raises(ValidationError, match="key must be nonempty"):
        ExternalBootPlan.model_validate(data)


@pytest.mark.parametrize("release", ["a" * 65, "café", "../evil", ".", ".."])
def test_release_values_reject_noncanonical_or_traversal_like_text(release: str) -> None:
    plan_data = _plan_data()
    obligation = cast(dict[str, object], plan_data["module_obligation"])
    plan_data["module_obligation"] = {**obligation, "release": release}

    with pytest.raises(ValidationError, match="release"):
        ExternalBootPlan.model_validate(plan_data)
    with pytest.raises(ValidationError, match="release"):
        RunningKernelObservation(architecture="x86_64", release=release, gnu_build_id="00" * 20)


@pytest.mark.parametrize("value", ["", "/tmp/kernel", "https://host/k", "user:secret@host"])
def test_opaque_provider_ref_rejects_empty_path_url_and_credentials(value: str) -> None:
    with pytest.raises(ValidationError):
        OpaqueProviderRef(ref=value)


@pytest.mark.parametrize(
    "value",
    ["kernel/a/../../secret", "kernel/../secret", "kernel\\secret", "C:secret", "ref\nforged"],
)
def test_opaque_provider_ref_rejects_destination_significant_syntax(value: str) -> None:
    with pytest.raises(ValidationError):
        OpaqueProviderRef(ref=value)


def test_canonical_deserialization_rejects_oversized_bytes_before_json_parsing() -> None:
    oversized = b"{" + b" " * 65_536 + b"not-json"

    with pytest.raises(ValueError, match="exceeds 65536 bytes"):
        ExternalBootPlan.from_canonical_json(oversized)


def test_recovery_point_binding_canonical_round_trip_and_closed_schema() -> None:
    provider = FaultInjectExternalBoot()
    authority = OpaqueProviderRef(ref="authority/current")
    materialization = provider.materialize(ExternalBootPlan.model_validate(_plan_data()), authority)
    binding = ExternalBootActivationBinding(
        system_id=SYSTEM_ID, run_id=RUN_ID, activation_id=ACTIVATION_ID
    )
    point = provider.prepare(materialization, binding, authority)

    assert RecoveryPoint.from_canonical_json(point.to_canonical_json()) == point
    assert point.binding == binding
    for mutation in ({"binding": None}, {"extra": True}, {"ownership": binding.model_dump()}):
        data = point.model_dump(by_alias=True)
        data.update(mutation)
        if "binding" not in mutation:
            data.pop("binding")
        with pytest.raises(ValidationError):
            RecoveryPoint.model_validate(data)


@pytest.mark.parametrize("field", ["system_id", "run_id"])
def test_prepare_rejects_materialization_owner_mismatch(field: str) -> None:
    provider = FaultInjectExternalBoot()
    authority = OpaqueProviderRef(ref="authority/current")
    materialization = provider.materialize(ExternalBootPlan.model_validate(_plan_data()), authority)
    values = {"system_id": SYSTEM_ID, "run_id": RUN_ID, "activation_id": ACTIVATION_ID}
    values[field] = "11111111-1111-1111-1111-111111111111"

    with pytest.raises(ValueError, match="does not match"):
        provider.prepare(materialization, ExternalBootActivationBinding(**values), authority)


def test_activation_binding_changes_recovery_point_identity() -> None:
    provider = FaultInjectExternalBoot()
    authority = OpaqueProviderRef(ref="authority/current")
    materialization = provider.materialize(ExternalBootPlan.model_validate(_plan_data()), authority)
    first = provider.prepare(
        materialization,
        ExternalBootActivationBinding(
            system_id=SYSTEM_ID, run_id=RUN_ID, activation_id=ACTIVATION_ID
        ),
        authority,
    )
    second = first.model_copy(
        update={
            "binding": ExternalBootActivationBinding(
                system_id=SYSTEM_ID,
                run_id=RUN_ID,
                activation_id="11111111-1111-1111-1111-111111111111",
            )
        }
    )
    assert first != second


def test_preparation_request_is_closed_and_binds_every_owner() -> None:
    plan = ExternalBootPlan.model_validate(_plan_data())
    request = ExternalBootPreparationRequest(
        phase="materialize",
        plan=plan,
        binding=ExternalBootActivationBinding(
            system_id=SYSTEM_ID, run_id=RUN_ID, activation_id=ACTIVATION_ID
        ),
        authority=OpaqueProviderRef(ref="authority/current"),
        operation_identity="materialize-1",
    )

    assert request.plan.ownership.system_id == request.binding.system_id
    assert request.plan.ownership.run_id == request.binding.run_id
    with pytest.raises(ValidationError):
        ExternalBootPreparationRequest.model_validate(
            {**request.model_dump(by_alias=True), "unexpected": True}
        )
    with pytest.raises(ValidationError, match="ownership"):
        ExternalBootPreparationRequest.model_validate(
            {
                **request.model_dump(by_alias=True),
                "binding": {
                    **request.binding.model_dump(),
                    "run_id": "11111111-1111-1111-1111-111111111111",
                },
            }
        )


def test_preparation_observation_enforces_phase_and_exact_binding() -> None:
    provider = FaultInjectExternalBoot()
    authority = OpaqueProviderRef(ref="authority/current")
    plan = ExternalBootPlan.model_validate(_plan_data())
    materialization = provider.materialize(plan, authority)
    binding = ExternalBootActivationBinding(
        system_id=SYSTEM_ID, run_id=RUN_ID, activation_id=ACTIVATION_ID
    )
    recovery = provider.prepare(materialization, binding, authority)

    assert (
        ExternalBootPreparationObservation(
            state="absent",
            binding=binding,
            plan_identity=plan.identity,
            authority=authority,
            operation_identity="materialize-1",
        ).state
        == "absent"
    )
    assert (
        ExternalBootPreparationObservation(
            state="materialized",
            binding=binding,
            plan_identity=plan.identity,
            authority=authority,
            operation_identity="materialize-1",
            materialization=materialization,
        ).materialization
        == materialization
    )
    assert (
        ExternalBootPreparationObservation(
            state="prepared",
            binding=binding,
            plan_identity=plan.identity,
            authority=authority,
            operation_identity="prepare-1",
            materialization=materialization,
            recovery_point=recovery,
        ).recovery_point
        == recovery
    )
    with pytest.raises(ValidationError, match="prepared"):
        ExternalBootPreparationObservation(
            state="prepared",
            binding=binding,
            plan_identity=plan.identity,
            authority=authority,
            operation_identity="prepare-1",
            materialization=materialization,
        )
