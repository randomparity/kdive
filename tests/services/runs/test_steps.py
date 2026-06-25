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


def test_refs_carry_no_modules_key_under_the_unified_format() -> None:
    # The combined `kernel` tar carries modules inside it; there is no separate modules ref to
    # expose (ADR-0234 §2). refs() advertises only kernel, vmlinux, and (when set) initrd.
    result = BuildStepResult(kernel_ref="k", debuginfo_ref="d", build_id="b", initrd_ref="i")
    assert result.refs() == {"kernel": "k", "vmlinux": "d", "initrd": "i"}
    assert "modules" not in result.refs()
