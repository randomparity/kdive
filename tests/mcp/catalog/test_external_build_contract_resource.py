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


def test_every_requirements_element_is_a_clause_object_keyed_by_symbols() -> None:
    # #1854: a requirements element stopped being a bare list of symbol names and became an
    # object, so that #1860 and #1859 can add `built_in` and `arch` keys beside `symbols`
    # without a second shape change. Every other assertion in this module reads
    # json.dumps(entry["requirements"]) and matches a substring, which passes identically against
    # ["KEXEC"] and against {"symbols": ["KEXEC"]} - so without this the shape change would land
    # with nothing holding it. Assert the shape structurally, on every clause of every feature.
    features = _doc()["feature_config_requirements"]["features"]
    assert features, "no features in the manifest, so the walk below would pass vacuously"

    clauses = [clause for f in features for clause in f["requirements"]]
    assert clauses, "no feature advertises a clause, so the walk below would pass vacuously"
    for clause in clauses:
        assert isinstance(clause, dict), clause
        # keys are bounded, not just present: an element carrying `built_in` or `arch` before
        # #1860/#1859 land would be a shape lie about a value the registry does not yet hold
        assert set(clause) == {"symbols"}, clause
        symbols = clause["symbols"]
        assert isinstance(symbols, list) and symbols, clause
        assert all(isinstance(s, str) and s for s in symbols), clause
        assert symbols == sorted(symbols), clause  # stable order for a diffable document

    kexec = next(
        c
        for f in features
        if f["feature"] == CRASH_CAPTURE
        for c in f["requirements"]
        if "KEXEC" in c["symbols"]
    )
    assert kexec == {"symbols": ["KEXEC"]}


def test_schema_version_is_two_for_the_clause_object_element() -> None:
    # The element-shape change bumps the contract document's schema_version once, in the first
    # change to land (#1854). Adding an optional key later does not bump it again, so a further
    # bump means the shape changed again and an agent has to be told.
    assert _doc()["schema_version"] == 2


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


def test_served_contract_advertises_the_sanitizer_tracing_and_coverage_features_ungated() -> None:
    # #1848: replaces #916/#917, which named the build-config catalog ADR-0316 deleted. The served
    # contract is the only surface an agent building its own kernel reads, so a feature that is
    # not here does not exist as far as that agent is concerned. All advertise-only: none of these
    # has an arming seam, so kdive never refuses a build on them.
    features = {f["feature"]: f for f in _doc()["feature_config_requirements"]["features"]}
    expected_symbols = {
        "kcsan": "KCSAN",
        "kfence": "KFENCE",
        "kmemleak": "DEBUG_KMEMLEAK",
        "lockdep": "PROVE_LOCKING",
        "ftrace": "FUNCTION_TRACER",
        "bpf_tracing": "DEBUG_INFO_BTF",
        "fault_injection": "FAULT_INJECTION",
        "kcov": "KCOV",
    }
    for feature_id, symbol in expected_symbols.items():
        assert feature_id in features, feature_id
        entry = features[feature_id]
        assert entry["gated"] is False, feature_id
        assert symbol in json.dumps(entry["requirements"]), feature_id
        # the manifest never leaks the internal refusal set
        assert "gate_required" not in entry, feature_id


def test_served_kasan_entry_states_what_it_finds_and_what_it_costs() -> None:
    # The whole point of #1848's kasan half: "Kernel Address Sanitizer instrumentation." gave an
    # agent no basis to size the guest or to choose inline over outline.
    kasan = next(
        f for f in _doc()["feature_config_requirements"]["features"] if f["feature"] == "kasan"
    )
    summary = kasan["summary"].lower()
    assert "use-after-free" in summary
    assert "1/8" in summary
    advertised = json.dumps(kasan["requirements"])
    assert "KASAN_GENERIC" in advertised
    assert "KASAN_HW_TAGS" in advertised
    # the instrumentation choice is unsettable under hardware tag-based mode, so it
    # is stated in prose rather than demanded as a requirement
    assert "KASAN_OUTLINE" not in advertised
    assert "KASAN_OUTLINE" in kasan["summary"]


def test_resource_reads_back_generated_json() -> None:
    app = FastMCP("external-build-contract-test")
    register(app, resolver=cast(ProviderResolver, _Resolver()))

    async def _read() -> str:
        result = await app.read_resource(EXTERNAL_BUILD_CONTRACT_URI)
        content = result.contents[0].content
        assert isinstance(content, str)
        return content

    assert asyncio.run(_read()) == external_build_contract_json()
