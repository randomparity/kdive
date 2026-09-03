"""A canonical external-boot plan every provider binding can start from (ADR-0583).

Providers override only what their own materialization needs; the plan stays a
provider-neutral ``ExternalBootPlan``.
"""

from __future__ import annotations

from typing import Any

ZERO_DIGEST = "sha256:" + "0" * 64
SYSTEM_ID = "00000000-0000-0000-0000-000000000003"
RUN_ID = "00000000-0000-0000-0000-000000000002"
BUILD_GENERATION = "00000000-0000-0000-0000-000000000001"
ACTIVATION_ID = "00000000-0000-0000-0000-000000000004"
RELEASE = "6.1.0"


def sample_plan_data(**overrides: Any) -> dict[str, Any]:
    """Return the shared canonical plan payload, with top-level keys overridden."""
    data: dict[str, Any] = {
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
            "release": RELEASE,
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
    data.update(overrides)
    return data
