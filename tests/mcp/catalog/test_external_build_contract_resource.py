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
    # #1854 (ADR-0544 §6): a requirements element stopped being a bare list of symbol names and
    # became an object, which is how #1860 and #1859 added `built_in` and `arch` keys beside
    # `symbols` without a second shape change. Every other assertion in this module reads
    # json.dumps(entry["requirements"]) and matches a substring, which passes identically against
    # ["KEXEC"] and against {"symbols": ["KEXEC"]} - so without this the shape change would land
    # with nothing holding it. Assert the shape structurally, on every clause of every feature.
    features = _doc()["feature_config_requirements"]["features"]
    assert features, "no features in the manifest, so the walk below would pass vacuously"

    clauses = [clause for f in features for clause in f["requirements"]]
    assert clauses, "no feature advertises a clause, so the walk below would pass vacuously"
    for clause in clauses:
        assert isinstance(clause, dict), clause
        # Keys are bounded, not just present: `built_in` joined the vocabulary with #1860 and
        # `arch` with #1859, and those three are the whole of it - ADR-0544's closing consequence
        # is that a fourth axis should arrive as an AND-ed prerequisite clause, not a fourth key,
        # so an element carrying one is a shape change an agent has to be told about.
        assert set(clause) <= {"symbols", "built_in", "arch"}, clause
        assert clause.get("built_in") in (None, "required", "unless_initrd"), clause
        symbols = clause["symbols"]
        assert isinstance(symbols, list) and symbols, clause
        assert all(isinstance(s, str) and s for s in symbols), clause
        assert symbols == sorted(symbols), clause  # stable order for a diffable document
        arch = clause.get("arch")
        if arch is not None:
            # Same three properties as `symbols`: a non-empty list of non-empty strings in a
            # stable order. An empty list would say "no arch", which is not what None says.
            assert isinstance(arch, list) and arch, clause
            assert all(isinstance(a, str) and a for a in arch), clause
            assert arch == sorted(arch), clause

    kexec = next(
        c
        for f in features
        if f["feature"] == CRASH_CAPTURE
        for c in f["requirements"]
        if "KEXEC" in c["symbols"]
    )
    assert kexec == {"symbols": ["KEXEC"]}  # and the key is omitted at its default


def test_the_served_document_carries_the_built_in_requirement_on_the_boot_clauses() -> None:
    # #1860. The whole point of the machine-readable array is that an agent diffs its config
    # against it, so a boot clause that needs =y has to say so *here* and not only in the summary
    # prose - that is the defect the issue reports on VIRTIO_PCI. Read the served document rather
    # than feature_manifest() so the value is pinned where the agent actually reads it.
    features = _doc()["feature_config_requirements"]["features"]
    rootfs = next(f for f in features if f["feature"] == "rootfs_mount")
    assert {"symbols": ["VIRTIO_BLK"], "built_in": "unless_initrd"} in rootfs["requirements"]
    console = next(f for f in features if f["feature"] == "serial_console")
    assert {"symbols": ["VIRTIO_PCI"], "built_in": "unless_initrd"} in console["requirements"]
    ikconfig = next(f for f in features if f["feature"] == "ikconfig")
    assert {"symbols": ["IKCONFIG"], "built_in": "required"} in ikconfig["requirements"]


def test_the_served_document_carries_the_arch_scope_on_the_console_clauses() -> None:
    # #1859 (ADR-0544 §3, §6). The clause-level tag is metadata the agent - who knows its own
    # target arch - filters on, and this is the surface it reads it from, so pin it on the served
    # document rather than on feature_manifest(). Before this, a ppc64le agent diffing its config
    # against the array was told to set SERIAL_8250_CONSOLE, which does nothing on pseries, and
    # was never told about HVC_CONSOLE at all - both halves of the issue, on one entry.
    features = _doc()["feature_config_requirements"]["features"]
    console = next(f for f in features if f["feature"] == "serial_console")
    assert console["requirements"] == [
        {"symbols": ["SERIAL_8250"], "built_in": "required", "arch": ["x86_64"]},
        {"symbols": ["SERIAL_8250_CONSOLE"], "arch": ["x86_64"]},
        {"symbols": ["HVC_CONSOLE"], "arch": ["ppc64le"]},
        {"symbols": ["VIRTIO_PCI"], "built_in": "unless_initrd"},
    ]


def test_no_other_served_feature_carries_an_arch_scope() -> None:
    # The key is omitted at its default, so the dozens of unscoped clauses stay as small as they
    # were and an agent filtering on `arch` sees only the one entry that really varies. Also the
    # served half of ADR-0544 §7's residual: crash_capture's arch-specific gated pair carries no
    # tag, deferred to #1875, so it must not appear here either.
    features = _doc()["feature_config_requirements"]["features"]
    tagged = {f["feature"] for f in features for clause in f["requirements"] if "arch" in clause}
    assert tagged == {"serial_console"}


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
