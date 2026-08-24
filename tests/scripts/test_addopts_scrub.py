"""The run's JUnit report path must not reach anything a test spawns (#2068, ADR-0578).

`.github/workflows/ci.yml` sets `PYTEST_ADDOPTS` for the whole `Test` step, so a nested pytest
inherits `--junit-xml=<shared path>` and writes its own report over the run's. When the outer
session never reaches `sessionfinish` — a cancelled job, an OOM-killed controller, a step
timeout — the summary step reads that leftover and reports a clean run.

These are nested-pytest runs because the behaviour cannot be observed in-process: the hook pops
at collection, before any test body runs, so a test that sets the variable itself and spawns a
child watches the child inherit it, and one that does not set it proves nothing. The nested
conftest imports the *real* hook from `tests._addopts_scrub`, so this exercises the shipped
implementation rather than a copy of it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_CONFTEST = """
import sys
sys.path.insert(0, {root!r})
from tests._addopts_scrub import pytest_collection  # noqa: F401  the hook under test
"""

_GRANDCHILD = """
import os
import subprocess
import sys


def _seen() -> str:
    result = subprocess.run(
        (sys.executable, "-c", "import os; print(os.environ.get('PYTEST_ADDOPTS'))"),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()
"""


def _run_nested(tmp_path: Path, module: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "conftest.py").write_text(_CONFTEST.format(root=str(_ROOT)), encoding="utf-8")
    (tmp_path / "test_inner.py").write_text(module, encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTEST_ADDOPTS"] = "--tb=long"
    return subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            str(tmp_path / "test_inner.py"),
            "-q",
            "-p",
            "no:cacheprovider",
        ),
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_a_process_spawned_from_a_test_does_not_inherit_addopts(tmp_path: Path) -> None:
    module = _GRANDCHILD + (
        "def test_grandchild():\n    observed = _seen()\n    assert observed == 'None', observed\n"
    )
    result = _run_nested(tmp_path, module)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_process_spawned_at_module_import_does_not_inherit_addopts(tmp_path: Path) -> None:
    # The case that distinguishes `pytest_collection` from a session-scoped fixture: a fixture
    # runs after test modules are imported, so this spawn would still inherit the value.
    module = _GRANDCHILD + (
        "_AT_IMPORT = _seen()\n"
        "def test_import_time():\n"
        "    assert _AT_IMPORT == 'None', _AT_IMPORT\n"
    )
    result = _run_nested(tmp_path, module)
    assert result.returncode == 0, result.stdout + result.stderr
