"""Shared exact external-boot plan fixture."""

from uuid import UUID

from kdive.providers.ports.external_boot import (
    ExternalBootMaterialization,
    ExternalBootPlan,
    MaterializedArtifacts,
    OpaqueProviderRef,
)


def external_boot_plan(system_id: UUID, run_id: UUID) -> ExternalBootPlan:
    zero = "sha256:" + "0" * 64
    return ExternalBootPlan.model_validate(
        {
            "architecture": "x86_64",
            "bundle": {
                "decoded_kernel_size_bytes": 200,
                "elf_metadata_bytes": 50,
                "gnu_build_id_size_bytes": 20,
                "key": "bundles/kernel.tar",
                "member_count": 2,
                "sha256": zero,
                "uncompressed_bytes": 101,
                "version": "v1",
                "vmlinuz_sha256": zero,
                "vmlinuz_size_bytes": 100,
            },
            "cmdline": "root=UUID=x",
            "debug_cmdline": None,
            "initrd": None,
            "module_obligation": {
                "member_count": 1,
                "release": "6.12.0",
                "source_manifest": zero,
                "uncompressed_bytes": 1,
            },
            "ownership": {
                "build_generation": "00000000-0000-0000-0000-000000000001",
                "run_id": str(run_id),
                "system_id": str(system_id),
            },
            "platform_arguments": ["root=UUID=x"],
            "root": {
                "architecture": "x86_64",
                "arguments": ["root=UUID=x"],
                "authority": "stage-inspection",
                "root": "UUID=x",
                "source": {"identity": zero, "kind": "staged-image"},
            },
        }
    )


def external_boot_materialization(plan: ExternalBootPlan) -> ExternalBootMaterialization:
    return ExternalBootMaterialization(
        architecture=plan.architecture,
        provider_kind="local-libvirt",
        ownership={
            "system_id": plan.ownership.system_id,
            "run_id": plan.ownership.run_id,
        },
        plan_identity=plan.identity,
        extracted_vmlinuz_sha256=plan.bundle.vmlinuz_sha256,
        source_module_manifest=plan.module_obligation.source_manifest,
        installed_module_tree="sha256:" + "3" * 64,
        verified_bundle_sha256=plan.bundle.sha256,
        verified_initrd_sha256=None,
        kernel_observation={
            "architecture": plan.architecture,
            "release": plan.module_obligation.release,
            "gnu_build_id": "01020304",
        },
        artifacts=MaterializedArtifacts(
            kernel=OpaqueProviderRef(ref="artifact/kernel"),
            modules=OpaqueProviderRef(ref="artifact/modules"),
            initrd=None,
        ),
    )
