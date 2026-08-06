"""External-build contract resource (#1579)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from fastmcp import FastMCP

from kdive.artifacts.read_model import RUN_ARTIFACT_NAMES, SYSTEM_ARTIFACT_NAMES
from kdive.build_artifacts.validation import EFFECTIVE_CONFIG_MAX_BYTES
from kdive.domain.catalog.resources import ResourceKind
from kdive.kernel_config.gate import MISSING_DEBUGINFO_REASON
from kdive.kernel_config.requirements import CRASH_CAPTURE, Enforcement
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
    assert crash["enforcement"] == Enforcement.UPLOAD_REFUSAL.value
    assert "RANDOMIZE_BASE" in json.dumps(crash["requirements"])
    # The refusal set reaches the agent under a served name; the internal field name does not.
    assert "gate_required" not in crash
    assert "RANDOMIZE_BASE" not in json.dumps(crash["refuses_on"])


def test_the_served_document_defines_every_enforcement_value_it_uses() -> None:
    # #1867. The defect was a served flag with no served definition: `gated` reached the agent
    # bare, so `gated: false` on sysrq read as "optional" when it means "the refusal is coming
    # later and costs a rebuild". A legend in a doc would be the same defect one fetch away, so
    # it rides inside the payload the agent already reads.
    manifest = _doc()["feature_config_requirements"]
    legend = manifest["enforcement_legend"]
    used = {f["enforcement"] for f in manifest["features"]}

    assert used, "no entry carries an enforcement value, so the checks below would pass vacuously"
    assert used <= set(legend)  # no value reaches an agent undefined
    assert set(legend) == {e.value for e in Enforcement}  # and none is defined without existing
    for value, definition in legend.items():
        assert isinstance(definition, str) and definition.strip(), value


def test_the_served_sysrq_entry_cannot_be_read_as_optional_beside_the_cheap_features() -> None:
    # The served half of #1867, on the surface the agent actually reads. #1861 correctly dropped
    # sysrq's unread refusal set, and with a bare bool that made the entry structurally identical
    # to kasan and its six siblings - so an agent skipping every `"gated": false` feature skipped
    # MAGIC_SYSRQ, a symbol with no module form, and paid a build, install and boot before
    # diagnostic_sysrq refused. Assert the misreading is closed: the values must differ, and the
    # key that grouped them must be gone rather than merely joined by a better one.
    features = {f["feature"]: f for f in _doc()["feature_config_requirements"]["features"]}
    sysrq = features["sysrq"]
    assert sysrq["enforcement"] == Enforcement.RUNTIME_REFUSAL.value
    for cheap in ("kasan", "kcsan", "kfence", "kmemleak", "lockdep", "ftrace", "kcov"):
        assert features[cheap]["enforcement"] == Enforcement.UNCHECKED.value, cheap
        assert features[cheap]["enforcement"] != sysrq["enforcement"], cheap
    for entry in features.values():
        assert "gated" not in entry, entry["feature"]


def test_every_requirements_element_is_a_clause_object_keyed_by_symbols() -> None:
    # #1854 (ADR-0544 §6): a requirements element stopped being a bare list of symbol names and
    # became an object, which is how #1860 and #1859 added `built_in` and `arch` keys beside
    # `symbols` without a second shape change. Every other assertion in this module reads
    # json.dumps(entry["requirements"]) and matches a substring, which passes identically against
    # ["KEXEC"] and against {"symbols": ["KEXEC"]} - so without this the shape change would land
    # with nothing holding it. Assert the shape structurally, on every clause of every feature.
    features = _doc()["feature_config_requirements"]["features"]
    assert features, "no features in the manifest, so the walk below would pass vacuously"

    # `refuses_on` renders through the same helper (#1867), so it is walked here rather than left
    # to grow a second element shape unobserved.
    clauses = [
        clause
        for f in features
        for key in ("requirements", "refuses_on")
        for clause in f.get(key, ())
    ]
    assert clauses, "no feature advertises a clause, so the walk below would pass vacuously"
    assert any("refuses_on" in f for f in features), "no refusal set reached the walk above"

    # An `also_checked` element embeds a clause object and adds three keys scoping it (ADR-0548
    # rule 1), so its clause half is walked with the other two rather than becoming the third
    # element shape - the outcome this test exists to prevent. Projected onto the clause keys
    # because the three scoped keys would otherwise fail the bounded-keys assertion below, which
    # is the point: `enforcement` on an element is a fact about kdive and is not a clause axis.
    scoped = [element for f in features for element in f.get("also_checked", ())]
    assert scoped, "no scoped statement reached the walk above"
    clauses += [
        {k: v for k, v in element.items() if k in ("symbols", "built_in", "arch")}
        for element in scoped
    ]

    # The entry's own key vocabulary is bounded for the same reason a clause's is: a new key is a
    # shape change an agent has to be told about, and `gated` must not come back (#1867).
    # `also_checked` joined with #1901, and is the whole of the addition.
    for entry in features:
        assert set(entry) <= {
            "feature",
            "summary",
            "enforcement",
            "requirements",
            "refuses_on",
            "also_checked",
        }, entry["feature"]

    # The scoped keys are bounded too, and each carries the type an agent branches on.
    for element in scoped:
        assert set(element) <= {
            "symbols",
            "built_in",
            "arch",
            "enforcement",
            "reason",
            "surfaces_at",
        }, element
        assert element["enforcement"] in {e.value for e in Enforcement}, element
        assert isinstance(element["reason"], str) and element["reason"], element
        tools = element["surfaces_at"]
        assert isinstance(tools, list) and tools, element
        assert all(isinstance(tool, str) and tool for tool in tools), element
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


def test_the_served_crash_capture_entry_scopes_the_symbols_no_ppc64le_kernel_can_set() -> None:
    # The served half of #1875, closing ADR-0544 §7's residual. Two of crash_capture's advertised
    # symbols are unreachable on ppc64le at any setting - FW_CFG_SYSFS, whose only powerpc
    # dependency arm PPC_PMAC itself depends on CPU_BIG_ENDIAN, and RANDOMIZE_BASE, which depends
    # on the 32-bit PPC_85xx. This document is where the agent reads that, so pin it here and not
    # only on feature_manifest(): a ppc64le agent diffing its config against the array was
    # previously told to set both, and then refused over the first.
    features = _doc()["feature_config_requirements"]["features"]
    crash = next(f for f in features if f["feature"] == "crash_capture")
    assert crash["requirements"] == [
        {"symbols": ["KEXEC"]},
        {"symbols": ["KEXEC_FILE"]},
        {"symbols": ["CRASH_DUMP"]},
        {"symbols": ["PROC_VMCORE"]},
        {"symbols": ["FW_CFG_SYSFS"], "arch": ["x86_64"]},
        {"symbols": ["RELOCATABLE"]},  # a real prompt on ppc64le, so it stays unscoped
        {"symbols": ["RANDOMIZE_BASE"], "arch": ["x86_64"]},
    ]


def test_no_other_served_feature_carries_an_arch_scope() -> None:
    # The key is omitted at its default, so the dozens of unscoped clauses stay as small as they
    # were and an agent filtering on `arch` sees only the entries that really vary. rootfs_mount
    # in particular must not appear: complete_build resolves no arch, so a tag there would make
    # its advisory vanish rather than fire (invariant I2, ADR-0544 §7).
    features = _doc()["feature_config_requirements"]["features"]
    tagged = {f["feature"] for f in features for clause in f["requirements"] if "arch" in clause}
    assert tagged == {"serial_console", "crash_capture"}


def test_schema_version_is_three_since_the_gated_bool_was_replaced() -> None:
    # 2 was the clause-object element (#1854), under a rule that an *added* optional key does not
    # bump again - which is how `built_in` (#1860) and `arch` (#1859) landed silently. #1867
    # removes a key every entry carried, which is the other case, so 3 is the signal an agent
    # keying on `gated` needs. A further bump means the shape changed again.
    assert _doc()["schema_version"] == 3


def test_served_contract_advertises_the_rhel_guest_kdump_symbols_ungated() -> None:
    # #1626: the symbols ADR-0213/ADR-0183 had put in the deleted kdump build-config fragment must
    # reach the agent through the one surface that replaced it. Advertised, never gated — kdive
    # cannot tell a RHEL guest from any other, so refusing on these would block installs that
    # capture fine elsewhere.
    features = _doc()["feature_config_requirements"]["features"]
    rhel = next(f for f in features if f["feature"] == "crash_capture_rhel_guest")
    assert rhel["enforcement"] == Enforcement.UNCHECKED.value
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
        assert entry["enforcement"] == Enforcement.UNCHECKED.value, feature_id
        assert symbol in json.dumps(entry["requirements"]), feature_id
        # the manifest never leaks the internal refusal set, and an unchecked entry has none
        assert "gate_required" not in entry, feature_id
        assert "refuses_on" not in entry, feature_id


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


def test_the_served_bpf_tracing_entry_states_the_warning_omitting_btf_draws() -> None:
    # #1901 on the surface the agent actually reads. The entry advertises five clauses and one of
    # them - DEBUG_INFO_BTF - draws a missing_debuginfo warning at the drgn-live seams, on a
    # kernel already built, installed and booted. Before ADR-0548 the served entry said only
    # `enforcement: unchecked`, whose legend reads "nothing here is verified before your build or
    # after it", so an agent skipping BTF to save a pahole pass had no served basis to know what
    # it was buying. Assert it can now decide from this payload alone.
    manifest = _doc()["feature_config_requirements"]
    entry = next(f for f in manifest["features"] if f["feature"] == "bpf_tracing")
    # The entry-level value is unchanged and still true of the four clauses no seam reads.
    assert entry["enforcement"] == Enforcement.UNCHECKED.value
    assert "refuses_on" not in entry
    (scoped,) = entry["also_checked"]
    assert scoped["symbols"] == ["DEBUG_INFO_BTF"]
    assert scoped["enforcement"] == Enforcement.RUNTIME_ADVISORY.value
    # the reason string is the one the warning payload carries, so the agent can correlate the
    # contract entry with the response it gets rather than pattern-matching prose
    assert scoped["reason"] == MISSING_DEBUGINFO_REASON
    assert scoped["surfaces_at"] == [
        "debug.start_session",
        "introspect.run",
        "introspect.script",
    ]
    # and the scoped value reaches the agent defined, which is the property #1867 was filed for
    # the absence of - a served flag with no served definition.
    assert Enforcement.RUNTIME_ADVISORY.value in manifest["enforcement_legend"]
    assert manifest["also_checked_legend"].strip()
    # non-vacuity: the served BTF clause must really be one of the five in `requirements`, not a
    # statement about a symbol the entry never advertises
    assert "DEBUG_INFO_BTF" in json.dumps(entry["requirements"])


def test_only_the_mixed_entry_carries_a_scoped_statement() -> None:
    # ADR-0548 rule 4's basis for leaving schema_version at 3: `also_checked` is absent from every
    # entry that has nothing to qualify, so an existing reader sees those entries unchanged. If a
    # second entry grows one, this fails and forces the version question to be asked again rather
    # than answered by the absence of anyone noticing.
    features = _doc()["feature_config_requirements"]["features"]
    carriers = [f["feature"] for f in features if "also_checked" in f]
    assert carriers == ["bpf_tracing"]
    assert len(features) > 1, "one entry in the document would make the check above vacuous"
    # the fifth vocabulary value is reachable only through that key, so a client that
    # exhaustively matches the entry-level `enforcement` never meets it
    assert Enforcement.RUNTIME_ADVISORY.value not in {f["enforcement"] for f in features}
    assert _doc()["schema_version"] == 3
