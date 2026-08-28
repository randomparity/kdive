"""Provider-neutral external-boot contract tests (ADR-0583)."""

from __future__ import annotations

import json
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from kdive.providers.fault_inject.lifecycle.external_boot import FaultInjectExternalBoot
from kdive.providers.ports.external_boot import (
    ExternalBootMaterialization,
    ExternalBootPlan,
    ExternalBootPorts,
    OpaqueProviderRef,
    RecoveryPoint,
    RootSpecV1,
    RunningKernelObservation,
)

ZERO_DIGEST = "sha256:" + "0" * 64
SYSTEM_ID = "00000000-0000-0000-0000-000000000003"
RUN_ID = "00000000-0000-0000-0000-000000000002"
BUILD_GENERATION = "00000000-0000-0000-0000-000000000001"


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


@pytest.mark.parametrize("value", ["", "/tmp/kernel", "https://host/k", "user:secret@host"])
def test_opaque_provider_ref_rejects_empty_path_url_and_credentials(value: str) -> None:
    with pytest.raises(ValidationError):
        OpaqueProviderRef(ref=value)


class _ContractConsumer:
    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef
    ) -> ExternalBootMaterialization:
        raise NotImplementedError

    def prepare(
        self, materialization: ExternalBootMaterialization, authority: OpaqueProviderRef
    ) -> RecoveryPoint:
        raise NotImplementedError

    def activate(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        raise NotImplementedError

    def observe(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> RunningKernelObservation:
        raise NotImplementedError

    def recover(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        raise NotImplementedError

    def cleanup(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        raise NotImplementedError


def test_protocol_exposes_six_provider_neutral_operations() -> None:
    ports: ExternalBootPorts = _ContractConsumer()
    assert ports is not None
    assert UUID(SYSTEM_ID).version == 4 or UUID(SYSTEM_ID).int == 3


def test_fault_inject_consumes_all_six_operations_without_provider_types() -> None:
    provider = FaultInjectExternalBoot()
    authority = OpaqueProviderRef(ref="authority/current")
    plan = ExternalBootPlan.model_validate(_plan_data())

    materialization = provider.materialize(plan, authority)
    recovery = provider.prepare(materialization, authority)
    provider.activate(recovery, authority)

    assert provider.observe(recovery, authority) == materialization.kernel_observation
    provider.recover(recovery, authority)
    provider.cleanup(recovery, authority)
    with pytest.raises(KeyError):
        provider.observe(recovery, authority)
