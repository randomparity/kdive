from kdive.services.runs.steps import BuildStepResult, _optional_str_list


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
    provenance = {
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


def test_load_ignores_build_provenance_with_non_string_values() -> None:
    loaded = BuildStepResult.load(
        {
            "kernel_ref": "k",
            "build_provenance": {"remote": "https://h/r", "resolved_commit": 123},
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
