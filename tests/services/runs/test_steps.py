from kdive.domain.lifecycle.run_steps import parse_boot_outcome
from kdive.images.families._fedora_customize import READINESS_MARKER
from kdive.services.runs.steps import (
    BuildStepResult,
    _optional_str,
    _optional_str_list,
    ready_boot_outcome,
)


def test_optional_str_passes_through_non_empty_string() -> None:
    assert _optional_str("kernel-ref") == "kernel-ref"


def test_optional_str_empty_string_is_none() -> None:
    # Empty string is falsy: coerced to None, not carried forward as "" (kills `and`->`or`).
    assert _optional_str("") is None


def test_optional_str_non_string_is_none() -> None:
    # A truthy non-string (e.g. an int) must not pass the isinstance-and-truthy gate.
    assert _optional_str(5) is None
    assert _optional_str(["not", "a", "str"]) is None


def test_ready_boot_outcome_descriptor_shape() -> None:
    # The success-path symmetry to the failure side: names what defined a clean boot's success.
    assert ready_boot_outcome() == {
        "outcome": "ready",
        "signal": "console_marker",
        "marker": "kdive-ready",
        "unit": "kdive-ready.service",
        "rule": "marker line reached with no pre-marker crash signature",
    }


def test_ready_boot_outcome_marker_sourced_from_readiness_constant() -> None:
    # Single-sourced from the image's readiness marker so marker/unit cannot drift.
    descriptor = ready_boot_outcome()
    assert descriptor["marker"] == READINESS_MARKER
    assert descriptor["unit"] == f"{READINESS_MARKER}.service"


def test_ready_boot_outcome_returns_a_fresh_mapping() -> None:
    # A fresh dict each call — a caller nesting it into a response cannot mutate the source.
    first = ready_boot_outcome()
    first["outcome"] = "tampered"
    assert ready_boot_outcome()["outcome"] == "ready"


def test_parse_boot_outcome_rejects_unknown_values() -> None:
    assert parse_boot_outcome("ready") == "ready"
    assert parse_boot_outcome("expected_crash_observed") == "expected_crash_observed"
    assert parse_boot_outcome("bogus") is None
    assert parse_boot_outcome(None) is None


def test_optional_str_list_passes_through_string_list() -> None:
    assert _optional_str_list(["console", "gdbstub"]) == ["console", "gdbstub"]


def test_optional_str_list_empty_list_is_empty_not_none() -> None:
    assert _optional_str_list([]) == []


def test_optional_str_list_rejects_non_list() -> None:
    assert _optional_str_list("console") is None


def test_optional_str_list_rejects_non_string_member() -> None:
    assert _optional_str_list(["console", 3]) is None


def test_initrd_ref_round_trips_through_dump_and_load() -> None:
    result = BuildStepResult(
        kernel_ref="k", debuginfo_ref="d", build_id="b", initrd_ref="runs/r/initrd"
    )
    dumped = result.dump()
    assert dumped["initrd_ref"] == "runs/r/initrd"
    assert BuildStepResult.load(dumped) == result


def test_build_provenance_round_trips_through_dump_and_load() -> None:
    provenance: dict[str, str | bool | list[str]] = {
        "remote": "https://git.kernel.org/pub/scm/linux.git",
        "ref": "v6.9",
        "resolved_commit": "a1b2c3d4",
        "build_host": "buildhost-1",
    }
    result = BuildStepResult(
        kernel_ref="k", debuginfo_ref="d", build_id="b", build_provenance=provenance
    )
    dumped = result.dump()
    assert dumped["build_provenance"] == provenance
    assert BuildStepResult.load(dumped) == result


def test_dump_omits_build_provenance_when_none() -> None:
    result = BuildStepResult(kernel_ref="k", debuginfo_ref="d", build_id="b")
    assert "build_provenance" not in result.dump()


def test_load_ignores_non_mapping_build_provenance() -> None:
    loaded = BuildStepResult.load(
        {"kernel_ref": "k", "debuginfo_ref": "d", "build_id": "b", "build_provenance": "nope"}
    )
    assert loaded is not None
    assert loaded.build_provenance is None


def test_load_ignores_build_provenance_with_non_string_non_bool_values() -> None:
    # An int (123) is neither str nor bool, so the whole map degrades to None. bool subclasses
    # int in Python, so this also guards against the coercion accidentally admitting ints.
    loaded = BuildStepResult.load(
        {
            "kernel_ref": "k",
            "build_provenance": {"remote": "https://h/r", "resolved_commit": 123},
        }
    )
    assert loaded is not None
    assert loaded.build_provenance is None


def test_build_provenance_round_trips_bool_dirty_flag() -> None:
    # The warm-tree lane carries dirty as a native bool (#861, ADR-0265); it must survive the
    # dump/load round-trip, not be dropped by a str-only coercion.
    provenance: dict[str, str | bool | list[str]] = {
        "label": "linux-6.9",
        "resolved_commit": "a1b2c3d4",
        "dirty": True,
        "tree_sha": "deadbeef",
    }
    result = BuildStepResult(
        kernel_ref="k", debuginfo_ref="d", build_id="b", build_provenance=provenance
    )
    dumped = result.dump()
    assert dumped["build_provenance"] == provenance
    loaded = BuildStepResult.load(dumped)
    assert loaded is not None
    assert loaded.build_provenance == provenance


def test_build_provenance_round_trips_dirty_files_list() -> None:
    # The warm-tree lane carries dirty_files as a JSON string array (#938, ADR-0282); it must
    # survive the dump/load round-trip, not be dropped by the str|bool-only coercion.
    provenance: dict[str, str | bool | list[str]] = {
        "label": "linux-6.9",
        "resolved_commit": "a1b2c3d4",
        "dirty": True,
        "untracked": False,
        "tree_sha": "deadbeef",
        "dirty_files": ["kernel/sched/core.c", "mm/slub.c"],
        "dirty_files_truncated": True,
    }
    result = BuildStepResult(
        kernel_ref="k", debuginfo_ref="d", build_id="b", build_provenance=provenance
    )
    dumped = result.dump()
    assert dumped["build_provenance"] == provenance
    loaded = BuildStepResult.load(dumped)
    assert loaded is not None
    assert loaded.build_provenance == provenance


def test_load_ignores_build_provenance_with_non_string_list_item() -> None:
    # A dirty_files list with a non-str element is malformed: the whole map degrades to None.
    loaded = BuildStepResult.load(
        {
            "kernel_ref": "k",
            "build_provenance": {"label": "x", "dirty_files": ["ok.c", 123]},
        }
    )
    assert loaded is not None
    assert loaded.build_provenance is None


def test_refs_carry_no_modules_key_under_the_unified_format() -> None:
    # The combined `kernel` tar carries modules inside it; there is no separate modules ref to
    # expose (ADR-0234 §2). refs() advertises only kernel, vmlinux, and (when set) initrd.
    result = BuildStepResult(kernel_ref="k", debuginfo_ref="d", build_id="b", initrd_ref="i")
    assert result.refs() == {"kernel": "k", "vmlinux": "d", "initrd": "i"}
    assert "modules" not in result.refs()
