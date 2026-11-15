"""The Python 3.15 lane stays informational and its run blocks do what they claim (ADR-0817, #3600).

The override and report steps are shell, so these tests execute them rather than match their text.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _ROOT / ".github/workflows"
_LANE = _WORKFLOWS / "python-315.yml"
_PROBES = ("lock", "sync", "ruff", "ty", "test")
_OVERRIDE = "Relax the 3.14-only pins for this lane"
_STEP_TIMEOUTS = {"lock": 10, "sync": 25, "test": 40}
_PROBE_GATE = {
    "sync": "steps.lock.outcome == 'success'",
    "ruff": "steps.sync.outcome == 'success'",
    "ty": "steps.sync.outcome == 'success'",
    "test": "steps.sync.outcome == 'success'",
}
_MARKER = "sys_platform == 'linux' and platform_machine == 'x86_64' and python_version == '3.15'"


def _workflow() -> dict:
    return yaml.safe_load(_LANE.read_text())


def _steps() -> list[dict]:
    return _workflow()["jobs"]["python-315"]["steps"]


def _step(name: str) -> dict:
    matches = [step for step in _steps() if step.get("name") == name]
    assert len(matches) == 1, f"expected one step named {name!r}, found {len(matches)}"
    return matches[0]


def _run(block: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", block],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _base_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in {"GITHUB_ENV", "LANE_LOGS"}}


def _relaxed(entries: list) -> list:
    return [e.replace("==", ">=", 1) if isinstance(e, str) else e for e in entries]


def test_override_relaxes_the_current_pyproject(tmp_path: Path) -> None:
    shutil.copy(_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    committed = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    env = {**_base_env(), "RUNNER_TEMP": str(tmp_path)}

    result = _run(_step(_OVERRIDE)["run"], tmp_path, env)

    assert result.returncode == 0, result.stderr
    data = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert data["project"]["requires-python"] == ">=3.14"
    assert data["project"]["dependencies"] == _relaxed(committed["project"]["dependencies"])
    groups = committed.get("dependency-groups", {})
    assert data.get("dependency-groups", {}).keys() == groups.keys()
    for name, entries in groups.items():
        assert data["dependency-groups"][name] == _relaxed(entries), f"group {name}"
    assert data["tool"]["uv"]["required-environments"] == [_MARKER]


def test_override_refuses_a_drifted_requires_python(tmp_path: Path) -> None:
    original = (_ROOT / "pyproject.toml").read_text()
    drifted = original.replace('requires-python = "==3.14.*"', 'requires-python = "==3.15.*"')
    assert drifted != original
    target = tmp_path / "pyproject.toml"
    target.write_text(drifted)
    env = {**_base_env(), "RUNNER_TEMP": str(tmp_path)}

    result = _run(_step(_OVERRIDE)["run"], tmp_path, env)

    assert result.returncode != 0
    assert "requires-python" in result.stderr
    assert target.read_text() == drifted


def test_report_lists_outcomes_and_never_fails(tmp_path: Path) -> None:
    block = _step("Report")["run"]
    logs = tmp_path / "lane"
    logs.mkdir()
    (logs / "lock.log").write_text(
        "Resolved 200 packages\nUpdated psycopg v3.3.4 -> v3.3.4, v3.3.9\n"
    )
    (logs / "sync.log").write_text("Prepared 3 packages\n      Built cffi==2.0.0\n")
    summary = tmp_path / "summary.md"
    outcomes = {
        "LOCK_OUTCOME": "success",
        "SYNC_OUTCOME": "success",
        "RUFF_OUTCOME": "success",
        "TY_OUTCOME": "failure",
        "TEST_OUTCOME": "skipped",
    }
    env = {**_base_env(), **outcomes, "LANE_LOGS": str(logs), "GITHUB_STEP_SUMMARY": str(summary)}

    result = _run(block, tmp_path, env)

    assert result.returncode == 0, result.stderr
    text = summary.read_text()
    rows = [line for line in text.splitlines() if line.startswith("| ") and "outcome" not in line]
    assert len(rows) == len(_PROBES)
    assert "| ty | failure |" in text
    assert "| test | skipped |" in text
    assert "Updated psycopg v3.3.4 -> v3.3.4, v3.3.9" in text
    assert "Built cffi==2.0.0" in text
    warnings = [line for line in result.stdout.splitlines() if line.startswith("::warning")]
    assert len(warnings) == 2, result.stdout

    bare = tmp_path / "bare.md"
    env = {
        **{k: v for k, v in _base_env().items() if not k.endswith("_OUTCOME")},
        "GITHUB_STEP_SUMMARY": str(bare),
    }
    result = _run(block, tmp_path, env)

    assert result.returncode == 0, result.stderr
    text = bare.read_text()
    assert text.count("not run") == len(_PROBES)
    assert text.count("(none)") == 2


def test_lane_is_informational_and_isolated() -> None:
    workflow = _workflow()
    # PyYAML reads the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert "push" not in triggers
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        assert "timeout-minutes" in job, f"job {name} has no timeout-minutes"
    job = workflow["jobs"]["python-315"]
    assert not job.get("needs")
    probes = {step["id"]: step for step in job["steps"] if step.get("id") in _PROBES}
    assert set(probes) == set(_PROBES)
    for probe, step in probes.items():
        assert step.get("continue-on-error") is True, f"probe {probe} can fail the job"
        assert step.get("if") == _PROBE_GATE.get(probe), f"probe {probe} chaining"
        assert step.get("timeout-minutes") == _STEP_TIMEOUTS.get(probe), f"probe {probe} timeout"
    report = _step("Report")
    assert report.get("if") == "always()"
    assert "continue-on-error" not in report
    failures = _step("Test failure summary")
    assert failures.get("continue-on-error") is True
    assert failures.get("if") == "always() && steps.test.outcome != 'skipped'"
    ci = (_WORKFLOWS / "ci.yml").read_text()
    assert "UV_PYTHON" not in ci
    assert "3.15" not in ci
