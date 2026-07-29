"""External-build contract resource (#1579)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from fastmcp import FastMCP

from kdive.artifacts.read_model import RUN_ARTIFACT_NAMES, SYSTEM_ARTIFACT_NAMES
from kdive.build_artifacts.validation import EFFECTIVE_CONFIG_MAX_BYTES
from kdive.domain.catalog.resources import ResourceKind
from kdive.kernel_config.requirements import CRASH_CAPTURE
from kdive.mcp.resources.external_build_contract import (
    EXTERNAL_BUILD_CONTRACT_URI,
    EXTERNAL_BUILD_UPLOAD_DOC,
    external_build_contract_document,
    external_build_contract_json,
)
from kdive.mcp.resources.registrar import register
from kdive.mcp.tools.catalog.artifacts.uploads import (
    CREATE_INVESTIGATION_UPLOAD_TOOL,
    CREATE_RUN_UPLOAD_TOOL,
)
from kdive.providers.core.resolver import ProviderResolver


class _Resolver:
    def registered_kinds(self) -> frozenset[ResourceKind]:
        return frozenset()


def _doc() -> dict[str, Any]:
    return cast(dict[str, Any], external_build_contract_document())


def _upload_contracts() -> dict[str, dict[str, Any]]:
    return cast(dict[str, dict[str, Any]], _doc()["upload_contracts"])


def test_contract_projects_both_owner_vocabularies() -> None:
    contracts = _upload_contracts()
    assert set(contracts) == {"run", "investigations"}

    run = contracts["run"]
    assert run["owner_kind"] == "run"
    assert run["accepted_names"] == sorted(RUN_ARTIFACT_NAMES)
    assert run["create_tool"] == CREATE_RUN_UPLOAD_TOOL
    assert set(run["contracts"]) == set(run["accepted_names"])

    investigations = contracts["investigations"]
    assert investigations["owner_kind"] == "investigations"
    assert investigations["accepted_names"] == sorted(SYSTEM_ARTIFACT_NAMES)
    assert investigations["create_tool"] == CREATE_INVESTIGATION_UPLOAD_TOOL
    assert investigations["accepted_names"] == ["rootfs"]
    assert set(investigations["contracts"]) == set(investigations["accepted_names"])


def test_run_contract_states_the_unified_provider_neutral_contract() -> None:
    run = _upload_contracts()["run"]
    assert run["provider_neutral"] is True
    assert run["doc"] == EXTERNAL_BUILD_UPLOAD_DOC

    kernel = run["contracts"]["kernel"]
    assert kernel["requirement"] == "required"
    assert kernel["format"]["container"] == "gzip tar"
    assert kernel["format"]["magic"] == [{"offset": 0, "hex": "1f8b"}]
    member_paths = {member["path"] for member in kernel["layout"]}
    assert member_paths == {"boot/vmlinuz", "lib/modules/"}
    boot = next(m for m in kernel["layout"] if m["path"] == "boot/vmlinuz")
    assert "format" not in boot
    assert set(boot["formats_by_arch"]) == {"x86_64", "ppc64le"}
    assert boot["formats_by_arch"]["x86_64"]["magic"] == [{"offset": 0x202, "hex": "48647253"}]
    assert boot["formats_by_arch"]["ppc64le"]["magic"] == [
        {"offset": 0, "hex": "7f454c460201"},  # pragma: allowlist secret
        {"offset": 18, "hex": "1500"},
    ]

    assert run["contracts"]["vmlinux"]["requirement"] == "optional"
    assert run["contracts"]["initrd"]["requirement"] == "optional"
    assert "build_id" in " ".join(run["contracts"]["vmlinux"]["notes"])

    effective = run["contracts"]["effective_config"]
    assert effective["requirement"] == "optional"
    assert effective["format"]["max_bytes"] == EFFECTIVE_CONFIG_MAX_BYTES
    notes = " ".join(effective["notes"])
    assert "never rejected" in notes
    assert "advisory" in notes


def test_feature_config_manifest_is_included_without_internal_gate_set() -> None:
    features = _doc()["feature_config_requirements"]["features"]
    ids = {f["feature"] for f in features}
    assert CRASH_CAPTURE in ids and "sysrq" in ids and "debuginfo" in ids

    crash = next(f for f in features if f["feature"] == CRASH_CAPTURE)
    assert crash["gated"] is True
    assert "RANDOMIZE_BASE" in json.dumps(crash["requirements"])
    assert "gate_required" not in crash


def test_served_contract_advertises_the_rhel_guest_kdump_symbols_ungated() -> None:
    # #1626: the symbols ADR-0213/ADR-0183 had put in the deleted kdump build-config fragment must
    # reach the agent through the one surface that replaced it. Advertised, never gated — kdive
    # cannot tell a RHEL guest from any other, so refusing on these would block installs that
    # capture fine elsewhere.
    features = _doc()["feature_config_requirements"]["features"]
    rhel = next(f for f in features if f["feature"] == "crash_capture_rhel_guest")
    assert rhel["gated"] is False
    advertised = json.dumps(rhel["requirements"])
    for symbol in (
        "XFS_FS",
        "SQUASHFS",
        "SQUASHFS_ZSTD",
        "EROFS_FS",
        "OVERLAY_FS",
        "BLK_DEV_LOOP",
        "KEXEC_FILE",
    ):
        assert symbol in advertised


def test_resource_reads_back_generated_json() -> None:
    app = FastMCP("external-build-contract-test")
    register(app, resolver=cast(ProviderResolver, _Resolver()))

    async def _read() -> str:
        result = await app.read_resource(EXTERNAL_BUILD_CONTRACT_URI)
        content = result.contents[0].content
        assert isinstance(content, str)
        return content

    assert asyncio.run(_read()) == external_build_contract_json()
