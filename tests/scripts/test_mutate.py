"""Behavioral tests for scripts/mutate.py (the `just mutate` wrapper).

mutmut itself is never run here; these test the wrapper's pure decision logic.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.mutate import (
    MARKER,
    MutateError,
    collect_results,
    format_summary,
    guard_no_existing_config,
    no_covered_mutants,
    parse_survivors,
    parse_total_mutants,
    preflight_collect,
    prepare_store,
    render_config,
    resolve_source,
    resolve_test_paths,
    run_mutmut,
    shim_source,
    signature,
    subprocess_env,
    write_signature,
)

_RESULTS = (
    "    kdive.domain.errors.xǁCategorizedErrorǁ__init____mutmut_1: survived\n"
    "    kdive.domain.errors.xǁCategorizedErrorǁ__init____mutmut_6: survived\n"
)
_RUN_TAIL = "⠇ 10/10  🎉 8 🫥 0  ⏰ 0  🤔 0  🙁 2  🔇 0  🧙 0\n200.09 mutations/second\n"


def test_resolve_source_returns_repo_relative_posix_path() -> None:
    assert resolve_source("src/kdive/domain/errors.py") == "src/kdive/domain/errors.py"


def test_resolve_source_rejects_path_outside_package() -> None:
    with pytest.raises(MutateError, match="under src/kdive"):
        resolve_source("scripts/mutate.py")


def test_resolve_source_rejects_missing_file() -> None:
    with pytest.raises(MutateError, match="does not exist"):
        resolve_source("src/kdive/domain/nope.py")


def test_resolve_source_rejects_directory() -> None:
    with pytest.raises(MutateError, match="must be a .py file"):
        resolve_source("src/kdive/domain")


def test_resolve_test_paths_accepts_existing_paths() -> None:
    assert resolve_test_paths(["tests/domain/test_errors.py"]) == ["tests/domain/test_errors.py"]


def test_resolve_test_paths_rejects_missing_path() -> None:
    with pytest.raises(MutateError, match="does not exist"):
        resolve_test_paths(["tests/domain/test_nope.py"])


def test_resolve_test_paths_rejects_empty_list() -> None:
    with pytest.raises(MutateError, match="at least one test path"):
        resolve_test_paths([])


def test_render_config_scopes_mutation_and_copies_whole_package() -> None:
    text = render_config("src/kdive/domain/errors.py", ["tests/domain/test_errors.py"])
    assert text.startswith(MARKER)
    assert "[mutmut]" in text
    # whole package copied so imports resolve; mutation scoped to the one file
    assert "source_paths=src/kdive\n" in text
    assert "only_mutate=src/kdive/domain/errors.py\n" in text
    assert "mutate_only_covered_lines=true\n" in text
    assert "max_stack_depth=8\n" in text


def test_render_config_uses_newline_token_test_selection_with_live_filter() -> None:
    text = render_config(
        "src/kdive/security/authz/gate.py",
        ["tests/security/authz", "tests/security/test_x.py"],
    )
    # each token on its own indented line so the marker expression stays one token
    assert "pytest_add_cli_args_test_selection=\n" in text
    assert "    -m\n" in text
    assert "    not live_vm and not live_stack\n" in text
    assert "    tests/security/authz\n" in text
    assert "    tests/security/test_x.py\n" in text


def test_render_config_suppresses_logger_but_not_raise() -> None:
    text = render_config("src/kdive/domain/errors.py", ["tests/domain/test_errors.py"])
    assert "do_not_mutate_patterns=logger.\\w+\n" in text
    assert "raise" not in text  # removed/weakened guard raises must stay mutable


def test_render_config_omits_redundant_also_copy() -> None:
    # mutmut force-appends tests/ and pyproject.toml to also_copy, so the wrapper
    # must not duplicate them; the also_copy block is dropped entirely.
    text = render_config("src/kdive/domain/errors.py", ["tests/domain/test_errors.py"])
    assert "also_copy" not in text


def test_parse_survivors_extracts_non_killed_names() -> None:
    assert parse_survivors(_RESULTS) == [
        ("kdive.domain.errors.xǁCategorizedErrorǁ__init____mutmut_1", "survived"),
        ("kdive.domain.errors.xǁCategorizedErrorǁ__init____mutmut_6", "survived"),
    ]


def test_parse_survivors_includes_timeout_and_suspicious() -> None:
    text = "    a.b_mutmut_1: timeout\n    a.b_mutmut_2: suspicious\n"
    assert parse_survivors(text) == [
        ("a.b_mutmut_1", "timeout"),
        ("a.b_mutmut_2", "suspicious"),
    ]


def test_parse_survivors_includes_no_tests_skipped_segfault() -> None:
    # regression guard: these non-killed statuses were silently dropped by the old
    # allow-list regex, under-reporting (in the worst case to "0 surviving").
    text = "    a: no tests\n    b: skipped\n    c: segfault\n"
    assert parse_survivors(text) == [
        ("a", "no tests"),
        ("b", "skipped"),
        ("c", "segfault"),
    ]


def test_parse_survivors_excludes_killed() -> None:
    text = "    a.b_mutmut_1: survived\n    a.b_mutmut_2: killed\n"
    assert parse_survivors(text) == [("a.b_mutmut_1", "survived")]


def test_parse_survivors_empty_when_all_killed() -> None:
    assert parse_survivors("") == []


def test_parse_total_mutants_reads_last_progress_token() -> None:
    assert parse_total_mutants(_RUN_TAIL) == 10


def test_parse_total_mutants_none_when_absent() -> None:
    assert parse_total_mutants("no progress here\n") is None


def test_format_summary_lists_survivors_and_count() -> None:
    out = format_summary(
        10,
        [("kdive.domain.errors.xǁCategorizedErrorǁ__init____mutmut_1", "no tests")],
        "src/kdive/domain/errors.py",
        ["tests/domain/test_errors.py"],
    )
    assert "10 mutants" in out
    assert "1 surviving" in out
    assert "kdive.domain.errors.xǁCategorizedErrorǁ__init____mutmut_1" in out
    assert "[no tests]" in out  # status label so the dev knows why it survived
    assert "mutmut show" in out  # tells the dev how to inspect
    assert "relative to the test path" in out


def test_format_summary_celebrates_zero_survivors() -> None:
    out = format_summary(10, [], "src/kdive/domain/errors.py", ["tests/domain/test_errors.py"])
    assert "0 surviving" in out


def test_signature_is_stable_for_same_target() -> None:
    a = signature("src/kdive/domain/errors.py", ["tests/domain/test_errors.py"])
    b = signature("src/kdive/domain/errors.py", ["tests/domain/test_errors.py"])
    assert a == b


def test_signature_differs_when_target_differs() -> None:
    a = signature("src/kdive/domain/errors.py", ["tests/domain/test_errors.py"])
    b = signature("src/kdive/domain/cost.py", ["tests/domain/test_errors.py"])
    assert a != b


def test_guard_refuses_existing_marked_config(tmp_path: Path) -> None:
    cfg = tmp_path / "setup.cfg"
    cfg.write_text("# kdive-mutate transient config — delete only when no run is in flight\n")
    with pytest.raises(MutateError, match="in flight"):
        guard_no_existing_config(cfg)


def test_guard_refuses_existing_foreign_config(tmp_path: Path) -> None:
    cfg = tmp_path / "setup.cfg"
    cfg.write_text("[flake8]\nmax-line-length = 100\n")
    with pytest.raises(MutateError, match="refusing to overwrite"):
        guard_no_existing_config(cfg)


def test_guard_passes_when_no_config(tmp_path: Path) -> None:
    guard_no_existing_config(tmp_path / "setup.cfg")  # no raise


def test_prepare_store_clears_on_target_change(tmp_path: Path) -> None:
    mutants = tmp_path / "mutants"
    mutants.mkdir()
    (mutants / ".kdive-target").write_text("old-signature")
    (mutants / "stale.json").write_text("{}")
    prepare_store("new-signature", mutants)
    assert not mutants.exists()


def test_prepare_store_keeps_on_same_target(tmp_path: Path) -> None:
    mutants = tmp_path / "mutants"
    mutants.mkdir()
    (mutants / ".kdive-target").write_text("sig")
    (mutants / "stats.json").write_text("{}")
    prepare_store("sig", mutants)
    assert (mutants / "stats.json").exists()


def test_prepare_store_clears_when_no_signature(tmp_path: Path) -> None:
    mutants = tmp_path / "mutants"
    mutants.mkdir()
    (mutants / "leftover.json").write_text("{}")  # broken prior run, no signature
    prepare_store("sig", mutants)
    assert not mutants.exists()


def test_write_signature_records_target(tmp_path: Path) -> None:
    mutants = tmp_path / "mutants"
    mutants.mkdir()
    write_signature("sig", mutants)
    assert (mutants / ".kdive-target").read_text() == "sig"


def _fake_runner(returncode: int, stdout: str = "", stderr: str = ""):
    def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    return run


def test_preflight_passes_when_tests_collected() -> None:
    preflight_collect(["tests/domain/test_errors.py"], runner=_fake_runner(0, "35 tests"))


def test_preflight_aborts_on_no_tests_collected() -> None:
    # pytest exit code 5 == "no tests collected"
    with pytest.raises(MutateError, match="no tests collected"):
        preflight_collect(["tests/x"], runner=_fake_runner(5))


def test_preflight_aborts_on_collection_error() -> None:
    with pytest.raises(MutateError, match="collection failed"):
        preflight_collect(["tests/x"], runner=_fake_runner(2, stderr="bad import"))


def test_run_mutmut_returns_stdout_on_success() -> None:
    out = run_mutmut(runner=_fake_runner(0, "10/10 done"))
    assert out == "10/10 done"


def test_run_mutmut_aborts_on_broken_baseline() -> None:
    with pytest.raises(MutateError, match="baseline"):
        run_mutmut(runner=_fake_runner(1, stderr="ModuleNotFoundError: kdive.config"))


def test_run_mutmut_treats_no_covered_mutants_as_benign() -> None:
    # mutmut exits non-zero when a target's only code runs at import (no covered, mutatable
    # lines under max_stack_depth) — that is a valid "0 mutants" result, not a broken baseline.
    stdout = (
        "done in 541ms (0 files mutated, 513 ignored, 1 unmodified)\n"
        "Stopping early, because we could not find any test case for any mutant.\n"
    )
    assert run_mutmut(runner=_fake_runner(1, stdout=stdout)) == stdout


def test_no_covered_mutants_detects_the_marker() -> None:
    assert no_covered_mutants(
        "Stopping early, because we could not find any test case for any mutant."
    )
    assert not no_covered_mutants("⠇ 10/10  🎉 8  🙁 2\n")


def test_collect_results_returns_stdout() -> None:
    assert collect_results(runner=_fake_runner(0, "a: survived\n")) == "a: survived\n"


def test_subprocess_env_sets_uv_no_sync() -> None:
    env = subprocess_env({}, "/tmp/shim")
    assert env["UV_NO_SYNC"] == "1"


def test_subprocess_env_uses_shim_dir_when_no_existing_pythonpath() -> None:
    env = subprocess_env({}, "/tmp/shim")
    assert env["PYTHONPATH"] == "/tmp/shim"


def test_subprocess_env_prepends_shim_dir_preserving_existing_pythonpath() -> None:
    env = subprocess_env({"PYTHONPATH": "/existing/a"}, "/tmp/shim")
    assert env["PYTHONPATH"] == f"/tmp/shim{os.pathsep}/existing/a"


def test_subprocess_env_preserves_other_base_keys_without_mutating_input() -> None:
    base = {"HOME": "/home/dev", "PYTHONPATH": "/x"}
    env = subprocess_env(base, "/tmp/shim")
    assert env["HOME"] == "/home/dev"
    # the builder must not mutate the caller's mapping in place
    assert base == {"HOME": "/home/dev", "PYTHONPATH": "/x"}


def test_shim_source_eager_imports_beartype_and_multiprocessing_under_guard() -> None:
    src = shim_source()
    assert "import multiprocessing" in src
    assert "beartype.claw._clawstate" in src
    assert "beartype.claw._importlib._clawimpload" in src
    assert "import pytest" in src
    # the eager imports must be best-effort so a missing optional dep never aborts startup
    assert "try:" in src
    assert "except Exception:" in src


def test_shim_source_is_valid_python() -> None:
    compile(shim_source(), "sitecustomize.py", "exec")
