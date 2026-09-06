from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest

from kdive.domain.errors import CategorizedError
from kdive.domain.lifecycle.records import InvestigationBuild
from kdive.providers.ports.external_boot import RootSource, RootSpecV1
from kdive.services.external_boot.plan import construct_external_boot_plan

_SHA = "sha256:" + "11" * 32


def _build() -> InvestigationBuild:
    return cast(
        "InvestigationBuild",
        type(
            "Build",
            (),
            {
                "generation": uuid4(),
                "canonical_document": {
                    "version": 2,
                    "external_boot_evidence": {
                        "schema": "external-boot-evidence-v1",
                        "architecture": "x86_64",
                        "bundle_sha256": _SHA,
                        "initrd": {"sha256": _SHA, "size_bytes": 1024},
                        "archive_member_count": 3,
                        "archive_uncompressed_bytes": 4096,
                        "vmlinuz_sha256": _SHA,
                        "vmlinuz_size_bytes": 2048,
                        "decoded_kernel_size_bytes": 4096,
                        "elf_metadata_bytes": 512,
                        "gnu_build_id_size_bytes": 8,
                        "release": "6.9.0-kdive",
                        "module_source_manifest": _SHA,
                        "module_member_count": 2,
                        "module_uncompressed_bytes": 64,
                    },
                },
                "artifacts": {
                    "kernel": {"version_id": "kernel-v1"},
                    "initrd": {"version_id": "initrd-v1"},
                },
                "build_result": {
                    "kernel_ref": "builds/kernel.tar",
                    "initrd_ref": "builds/initrd.img",
                },
            },
        )(),
    )


def _root() -> RootSpecV1:
    return RootSpecV1(
        architecture="x86_64",
        root="/dev/vda1",
        arguments=("root=/dev/vda1",),
        authority="stage-inspection",
        source=RootSource(kind="staged-image", identity=_SHA),
    )


def test_construct_plan_uses_only_versioned_build_and_root_facts() -> None:
    build = _build()
    system_id, run_id = uuid4(), uuid4()
    plan = construct_external_boot_plan(
        build=build,
        system_id=system_id,
        run_id=run_id,
        root=_root(),
        platform_arguments=("root=/dev/vda1", "console=ttyS0"),
        debug_cmdline="panic=1",
    )

    assert plan.ownership.build_generation == str(build.generation)
    assert plan.bundle.version == "kernel-v1"
    assert plan.initrd is not None and plan.initrd.version == "initrd-v1"
    assert plan.cmdline == "root=/dev/vda1 console=ttyS0 panic=1"


def test_construct_plan_fails_closed_without_v2_evidence() -> None:
    build = _build()
    build.canonical_document["version"] = 1

    with pytest.raises(CategorizedError, match="rebuild the Run") as raised:
        construct_external_boot_plan(
            build=build,
            system_id=uuid4(),
            run_id=uuid4(),
            root=_root(),
            platform_arguments=("root=/dev/vda1",),
            debug_cmdline=None,
        )
    assert raised.value.details["reason"] == "external_boot_evidence_missing"
