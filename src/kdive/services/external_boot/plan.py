"""Pure construction of an external-boot plan from immutable build facts (ADR-0613)."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from pydantic import ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import InvestigationBuild
from kdive.providers.ports.external_boot import ExternalBootPlan, RootSpecV1
from kdive.serialization import JsonValue


def construct_external_boot_plan(
    *,
    build: InvestigationBuild,
    system_id: UUID,
    run_id: UUID,
    root: RootSpecV1,
    platform_arguments: tuple[str, ...],
    debug_cmdline: str | None,
) -> ExternalBootPlan:
    """Validate and freeze one plan without reading mutable provider state."""
    document = build.canonical_document
    evidence = document.get("external_boot_evidence")
    artifacts = build.artifacts
    result = build.build_result
    if document.get("version") != 2 or not isinstance(evidence, dict):
        raise _invalid("external_boot_evidence_missing")
    kernel = artifacts.get("kernel")
    initrd = artifacts.get("initrd")
    if kernel is None or not isinstance(result.get("kernel_ref"), str):
        raise _invalid("external_boot_kernel_missing")
    data: dict[str, JsonValue] = {
        "schema": "external-boot-plan-v1",
        "architecture": evidence.get("architecture"),
        "ownership": {
            "system_id": str(system_id),
            "run_id": str(run_id),
            "build_generation": str(build.generation),
        },
        "bundle": {
            "key": result["kernel_ref"],
            "version": kernel.get("version_id"),
            "sha256": evidence.get("bundle_sha256"),
            "vmlinuz_sha256": evidence.get("vmlinuz_sha256"),
            "member_count": evidence.get("archive_member_count"),
            "uncompressed_bytes": evidence.get("archive_uncompressed_bytes"),
            "vmlinuz_size_bytes": evidence.get("vmlinuz_size_bytes"),
            "decoded_kernel_size_bytes": evidence.get("decoded_kernel_size_bytes"),
            "elf_metadata_bytes": evidence.get("elf_metadata_bytes"),
            "gnu_build_id_size_bytes": evidence.get("gnu_build_id_size_bytes"),
        },
        "initrd": None,
        "cmdline": " ".join(platform_arguments)
        + (f" {debug_cmdline}" if debug_cmdline is not None else ""),
        "debug_cmdline": debug_cmdline,
        "platform_arguments": list(platform_arguments),
        "module_obligation": {
            "release": evidence.get("release"),
            "source_manifest": evidence.get("module_source_manifest"),
            "member_count": evidence.get("module_member_count"),
            "uncompressed_bytes": evidence.get("module_uncompressed_bytes"),
        },
        "root": root.model_dump(mode="json", by_alias=True),
    }
    initrd_evidence = evidence.get("initrd")
    initrd_ref = result.get("initrd_ref")
    if initrd_evidence is not None:
        if (
            not isinstance(initrd_evidence, dict)
            or initrd is None
            or not isinstance(initrd_ref, str)
        ):
            raise _invalid("external_boot_initrd_incomplete")
        data["initrd"] = {
            "key": initrd_ref,
            "version": initrd.get("version_id"),
            "sha256": initrd_evidence.get("sha256"),
            "size_bytes": initrd_evidence.get("size_bytes"),
        }
    try:
        return ExternalBootPlan.model_validate(cast("object", data))
    except ValidationError as exc:
        raise _invalid("external_boot_plan_invalid") from exc


def _invalid(reason: str) -> CategorizedError:
    return CategorizedError(
        "immutable build evidence cannot construct an external-boot plan; rebuild the Run",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": reason},
    )


__all__ = ["construct_external_boot_plan"]
