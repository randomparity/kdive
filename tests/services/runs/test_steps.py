from kdive.services.runs.steps import BuildStepResult, _optional_str_list


def test_optional_str_list_passes_through_string_list() -> None:
    assert _optional_str_list(["console", "gdbstub"]) == ["console", "gdbstub"]


def test_optional_str_list_empty_list_is_empty_not_none() -> None:
    assert _optional_str_list([]) == []


def test_optional_str_list_rejects_non_list() -> None:
    assert _optional_str_list("console") is None


def test_optional_str_list_rejects_non_string_member() -> None:
    assert _optional_str_list(["console", 3]) is None


def test_modules_ref_round_trips_through_dump_and_load() -> None:
    result = BuildStepResult(
        kernel_ref="k", debuginfo_ref="d", build_id="b", modules_ref="runs/r/modules"
    )
    dumped = result.dump()
    assert dumped["modules_ref"] == "runs/r/modules"
    assert BuildStepResult.load(dumped) == result


def test_modules_ref_absent_is_omitted_and_loads_none() -> None:
    result = BuildStepResult(kernel_ref="k", debuginfo_ref="d", build_id="b")
    assert "modules_ref" not in result.dump()
    loaded = BuildStepResult.load(result.dump())
    assert loaded is not None
    assert loaded.modules_ref is None


def test_refs_exposes_modules_under_modules_key() -> None:
    result = BuildStepResult(kernel_ref="k", debuginfo_ref="d", build_id="b", modules_ref="m")
    assert result.refs()["modules"] == "m"
