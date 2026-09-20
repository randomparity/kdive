"""Behavioral coverage for the shared live-tier proof predicate (#2584)."""

from __future__ import annotations

import subprocess
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
