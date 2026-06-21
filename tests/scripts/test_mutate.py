"""Behavioral tests for scripts/mutate.py (the `just mutate` wrapper).

mutmut itself is never run here; these test the wrapper's pure decision logic.
"""

from __future__ import annotations

import pytest

from scripts.mutate import (
    MARKER,
    MutateError,
    render_config,
    resolve_source,
    resolve_test_paths,
)


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


def test_render_config_copies_pyproject_and_tests() -> None:
    text = render_config("src/kdive/domain/errors.py", ["tests/domain/test_errors.py"])
    assert "also_copy=\n    pyproject.toml\n    tests\n" in text
