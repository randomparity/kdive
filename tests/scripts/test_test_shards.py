"""The CI partitions must preserve the ordinary suite and its execution settings."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _command(tmp_path: Path, recipe: list[str]) -> subprocess.CompletedProcess[str]:
    just = shutil.which("just")
    if just is None:
        pytest.skip("just is required to expand the test recipes")
    executable = tmp_path / "uv"
    executable.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "print(json.dumps([os.environ['PYTHONHASHSEED'], sys.argv[1:]]))\n"
    )
    executable.chmod(0o755)
    return subprocess.run(
        [just, "--justfile", str(_ROOT / "justfile"), *recipe],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "PYTHONHASHSEED": "17"},
        capture_output=True,
        text=True,
        check=False,
    )


def _arguments(tmp_path: Path, recipe: list[str]) -> list[str]:
    result = _command(tmp_path, recipe)
    assert result.returncode == 0, result.stderr
    seed, arguments = json.loads(result.stdout)
    assert seed == "17"
    return arguments


def test_shards_preserve_gate_flags_and_partition_new_paths(tmp_path: Path) -> None:
    whole = _arguments(tmp_path, ["test"])
    selections = []
    for name in ("mcp-db", "other"):
        shard = _arguments(tmp_path, ["test-shard", name])
        assert shard[: len(whole)] == whole
        selections.append(shard[len(whole) :])

    suite = tmp_path / "suite"
    for path in ("db", "mcp", "providers", "new_family", ""):
        directory = suite / "tests" / path
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"test_{path or 'top'}.py").write_text(
            "import pytest\ndef test_ordinary(): pass\n"
            "@pytest.mark.live_vm\ndef test_live(): pass\n"
            "@pytest.mark.live_stack\ndef test_stack(): pass\n"
            "@pytest.mark.agent_smoke\ndef test_agent(): pass\n"
        )
    marker = whole[whole.index("-m", whole.index("pytest") + 1) + 1]

    def collect(paths: list[str]) -> set[str]:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", marker, *paths],
            cwd=suite,
            env={**os.environ, "PYTEST_ADDOPTS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            capture_output=True,
            text=True,
            check=True,
        )
        return {
            line
            for line in result.stdout.splitlines()
            if line.startswith("tests/") and "::" in line
        }

    all_tests = collect(["tests"])
    first, second = (collect(paths) for paths in selections)
    assert len(all_tests) == 5
    assert not first & second
    assert first | second == all_tests
    assert any("new_family" in node for node in second)
    assert any("test_top.py" in node for node in second)


def test_unknown_shard_fails_before_pytest(tmp_path: Path) -> None:
    result = _command(tmp_path, ["test-shard", "misspelled"])
    assert result.returncode == 2
    assert not result.stdout
    assert "expected shard" in result.stderr
