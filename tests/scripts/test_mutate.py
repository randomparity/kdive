"""Behavioral tests for scripts/mutate.py (the `just mutate` wrapper).

mutmut itself is never run here; these test the wrapper's pure decision logic.
"""

from __future__ import annotations

import pytest

from scripts.mutate import (
    MutateError,
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
