"""Behavioral coverage for the shared live-tier proof predicate (#2584)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_CHECKER = Path(__file__).resolve().parents[2] / "scripts" / "pytest-terminal-summary-has-passes.sh"


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        ("SKIPPED [1] test.py: needs a prior run where 1 passed\n4 skipped in 0.01s\n", False),
        ("2 failed, 4 skipped in 0.01s\n", False),
        ("no tests ran in 0.01s\n", False),
        ("1 passed in 0.01s\n", True),
        ("1 failed, 2 passed, 3 skipped in 0.01s\n", True),
        ("\x1b[32m1 passed in 0.01s\x1b[0m\n", True),
    ],
)
def test_checker_only_accepts_a_passing_terminal_summary(
    tmp_path: Path, stream: str, expected: bool
) -> None:
    summary = tmp_path / "summary"
    summary.write_text(stream)
    result = subprocess.run([str(_CHECKER), str(summary)], check=False)
    assert (result.returncode == 0) is expected


def test_checker_accepts_colored_summary_under_utf8_locale(tmp_path: Path) -> None:
    summary = tmp_path / "summary"
    summary.write_text("\x1b[32m13 passed, 1 failed in 1.23s\x1b[0m\n")

    result = subprocess.run(
        [str(_CHECKER), str(summary)],
        check=False,
        env={**os.environ, "LC_ALL": "en_US.UTF-8"},
    )

    assert result.returncode == 0


@pytest.mark.parametrize("width", [40, 120])
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("def test_proof():\n    assert True\n", True),
        (
            'import pytest\n\ndef test_no_proof():\n    pytest.skip("previous run had 1 passed")\n',
            False,
        ),
    ],
)
def test_checker_handles_real_pytest_output_at_varied_widths(
    tmp_path: Path, width: int, body: str, expected: bool
) -> None:
    test_file = tmp_path / "test_proof.py"
    test_file.write_text(body, encoding="utf-8")
    run = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q", "-ra", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "COLUMNS": str(width)},
    )
    assert run.returncode == 0, run.stderr
    summary = tmp_path / f"summary-{width}"
    summary.write_text(run.stdout, encoding="utf-8")
    result = subprocess.run([str(_CHECKER), str(summary)], check=False)
    assert (result.returncode == 0) is expected
