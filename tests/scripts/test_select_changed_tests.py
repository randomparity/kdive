"""The inner-loop changed-test selector (ADR-0420, issue #1334).

``scripts/select_changed_tests.py`` maps a git diff to the pytest targets a change
touches, or signals the full suite when a change is unmappable. The mapping logic is a
pure function, ``select_targets``, driven here with injected inputs (no git repo needed),
the way ``schema_immutable_guard.find_violations`` is tested.

Contract of ``select_targets(changed, test_index)``:

- returns ``None``  → the change set is unmappable; the caller runs the full suite
- returns ``[]``    → no changed tests; nothing to run
- returns ``[...]`` → the exact, de-duplicated, sorted pytest target paths
"""

from __future__ import annotations

from scripts.select_changed_tests import build_test_index, select_targets

# A small fake tree: stem -> the existing tests/**/test_<stem>.py paths on disk.
_INDEX = {
    "errors": ["tests/domain/test_errors.py"],
    "control": ["tests/mcp/test_control.py", "tests/providers/test_control.py"],
    "repositories": ["tests/db/test_repositories.py"],
    "select_changed_tests": ["tests/scripts/test_select_changed_tests.py"],
}


def test_changed_source_file_maps_to_its_named_test() -> None:
    assert select_targets(["src/kdive/domain/errors.py"], _INDEX) == ["tests/domain/test_errors.py"]


def test_changed_source_file_runs_every_basename_match() -> None:
    # A stem with duplicate basenames across the tree runs all of them (safe over-run).
    assert select_targets(["src/kdive/mcp/tools/control.py"], _INDEX) == [
        "tests/mcp/test_control.py",
        "tests/providers/test_control.py",
    ]


def test_changed_test_file_is_a_direct_target() -> None:
    assert select_targets(["tests/domain/test_errors.py"], _INDEX) == [
        "tests/domain/test_errors.py"
    ]


def test_source_file_with_zero_named_tests_falls_back_to_full_suite() -> None:
    # A changed source file we can't map is the false-confidence risk -> run everything.
    assert select_targets(["src/kdive/domain/brand_new_module.py"], _INDEX) is None


def test_changed_conftest_falls_back_to_full_suite() -> None:
    # conftest.py is under tests/ but is not a test_* file; its blast radius is a subtree.
    assert select_targets(["tests/domain/conftest.py"], _INDEX) is None


def test_non_python_change_falls_back_to_full_suite() -> None:
    assert select_targets(["pyproject.toml"], _INDEX) is None
    assert select_targets(["justfile"], _INDEX) is None
    assert select_targets(["docs/adr/0420-fast-inner-loop-test-recipes.md"], _INDEX) is None


def test_mixed_set_with_one_unmappable_file_falls_back() -> None:
    # A mappable test change plus one unmappable change -> the whole run is the full suite.
    changed = ["tests/domain/test_errors.py", "pyproject.toml"]
    assert select_targets(changed, _INDEX) is None


def test_deleted_test_file_is_ignored_not_a_target() -> None:
    # A path that looks like a test file but is absent from the index (deleted / never
    # existed) is not passed to pytest — that would make pytest error on a missing path.
    assert select_targets(["tests/domain/test_gone.py"], _INDEX) == []


def test_empty_change_set_runs_nothing() -> None:
    assert select_targets([], _INDEX) == []


def test_targets_are_deduplicated_and_sorted() -> None:
    # The same test reached via a source change and a direct test change appears once.
    changed = ["src/kdive/domain/errors.py", "tests/domain/test_errors.py"]
    assert select_targets(changed, _INDEX) == ["tests/domain/test_errors.py"]


def test_suffix_style_test_files_are_recognized() -> None:
    index = {"widget": ["tests/ui/widget_test.py"]}
    assert select_targets(["tests/ui/widget_test.py"], index) == ["tests/ui/widget_test.py"]


def test_build_test_index_keys_by_stem(tmp_path) -> None:
    # Real filesystem walk: both naming styles land under the right stem.
    tests_dir = tmp_path / "tests" / "domain"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_errors.py").write_text("")
    (tests_dir / "widget_test.py").write_text("")
    (tests_dir / "conftest.py").write_text("")  # not a test file — excluded
    index = build_test_index(tmp_path)
    assert index["errors"] == ["tests/domain/test_errors.py"]
    assert index["widget"] == ["tests/domain/widget_test.py"]
    assert "conftest" not in index
