"""The required CI check must represent every gate after splitting independent jobs."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github/workflows/ci.yml"
_WORK_JOBS = {"checks", "tests", "ansible", "compose-volumes"}
_RESULTS = ("CHECKS_RESULT", "TESTS_RESULT", "ANSIBLE_RESULT", "COMPOSE_RESULT")


def _jobs() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text())["jobs"]


def test_required_check_waits_for_all_independent_jobs_even_after_failure() -> None:
    jobs = _jobs()
    gate = jobs["lint-type-test"]
    assert gate["name"] == "lint · type · test"
    assert set(gate["needs"]) == _WORK_JOBS
    assert gate["if"] == "always()"
    assert not gate.get("continue-on-error", False)
    assert len(gate["steps"]) == 1
    step = gate["steps"][0]
    assert not step.get("if")
    assert not step.get("continue-on-error", False)
    assert step["shell"] == "bash"
    assert step["env"] == {
        "CHECKS_RESULT": "${{ needs.checks.result }}",
        "TESTS_RESULT": "${{ needs.tests.result }}",
        "ANSIBLE_RESULT": "${{ needs.ansible.result }}",
        "COMPOSE_RESULT": "${{ needs.compose-volumes.result }}",
    }
    for name in _WORK_JOBS:
        job = jobs[name]
        assert not job.get("needs"), f"{name} must start independently"
        assert not job.get("if"), f"{name} must not silently skip its gates"
        assert not job.get("continue-on-error", False)
        for step in job["steps"]:
            # Only test reporting is advisory; the underlying gate remains mandatory.
            reporting = step["name"] in {"Test failure summary", "Retain test timings and failures"}
            assert not step.get("continue-on-error", False) or reporting


@pytest.mark.parametrize("variable", _RESULTS)
@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped", "", None])
def test_aggregate_command_requires_explicit_success(variable: str, result: str | None) -> None:
    step = _jobs()["lint-type-test"]["steps"][0]
    environment = {**os.environ, **dict.fromkeys(_RESULTS, "success")}
    if result is None:
        environment.pop(variable)
    else:
        environment[variable] = result
    completed = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == (0 if result == "success" else 1), completed.stderr
    assert all(name in completed.stdout for name in _RESULTS)


def test_all_original_gate_recipes_have_one_owner() -> None:
    expected = {
        "checks": {
            "build",
            "lint",
            "lint-shell",
            "type",
            "lock-check",
            "coverage-check",
            "docs-links",
            "docs-paths",
            "served-doc-links",
            "adr-status-check",
            "docs-check",
            "config-docs-check",
            "config-guard",
            "env-docs-check",
            "migration-order-check",
            "schema-guard",
            "resources-docs-check",
            "doc-constants-check",
            "mcp-spec-check",
            "chart-version-check",
            "cli-verbs-check",
        },
        "tests": {"pull-test-images", "test-shard"},
        "ansible": {"install-ansible-collections", "lint-ansible", "test-ansible"},
        "compose-volumes": {"test-compose-volumes"},
    }
    for name, recipes in expected.items():
        steps = _jobs()[name]["steps"]
        actual = [
            recipe
            for step in steps
            for recipe in re.findall(r"^just ([\w-]+)", step.get("run", ""), re.MULTILINE)
        ]
        assert set(actual) == recipes
        assert len(actual) == len(recipes), f"duplicate gates in {name}"
        for step in steps:
            if re.search(r"^just (?!install-ansible-collections)[\w-]+", step.get("run", "")):
                assert not step.get("if"), f"conditional gate: {name}/{step['name']}"


def test_split_jobs_carry_their_own_runtime_and_base_ref_prerequisites() -> None:
    jobs = _jobs()
    for name in _WORK_JOBS:
        steps = jobs[name]["steps"]
        names = [step["name"] for step in steps]
        assert names.index("Install libvirt build headers") < names.index("Sync dependencies")
        assert names.index("Set up uv") < names.index("Sync dependencies")
        assert names.index("Sync dependencies") < names.index("Set up just")
    tests = jobs["tests"]["steps"]
    names = [step["name"] for step in tests]
    assert names.index("Set up Helm") < names.index("Test")
    test = next(step for step in tests if step["name"] == "Test")
    assert test["env"]["KDIVE_REQUIRE_DOCKER"] == "1"
    checks = jobs["checks"]["steps"]
    names = [step["name"] for step in checks]
    assert names.index("Fetch the base branch for the migration guards") < names.index(
        "Migration ordering guard"
    )
    assert names.index("Fetch this PR's base commit for the immutability guard") < names.index(
        "Schema immutability guard"
    )
    schema = next(step for step in checks if step["name"] == "Schema immutability guard")
    assert schema["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert '"${BASE_SHA:-origin/main}"' in schema["run"]


@pytest.mark.parametrize("fail_at", [None, 0, 4, 8])
def test_ansible_timings_preserve_failure_and_stop_later_harnesses(
    tmp_path: Path, fail_at: int | None
) -> None:
    just = shutil.which("just")
    if just is None:
        pytest.skip("just is required to exercise its recipe")
    harnesses = [
        "run-libvirt-stack-families.sh",
        "run-gdbstub-acl-prune.sh",
        "run-github-runner-preflight.sh",
        "run-guest-base-image-admission.sh",
        "run-remote-libvirt-facts-render.sh",
        "run-remote-module-appliance.sh",
        "run-external-boot-recovery-root.sh",
        "run-local-worker-host.py",
        "run-local-libvirt-host.py",
    ]
    stub = tmp_path / "uv"
    stub.write_text(
        '#!/bin/bash\nname="${!#}"\nname="${name##*/}"\n'
        'echo "$name" >> "$HARNESS_LOG"\n'
        'if [[ "$name" == "$FAIL_HARNESS" ]]; then exit 42; fi\n'
    )
    stub.chmod(0o755)
    log = tmp_path / "harnesses.log"
    completed = subprocess.run(
        [just, "--justfile", str(_ROOT / "justfile"), "test-ansible"],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "HARNESS_LOG": str(log),
            "FAIL_HARNESS": "none" if fail_at is None else harnesses[fail_at],
        },
        capture_output=True,
        text=True,
        check=False,
    )
    expected = harnesses if fail_at is None else harnesses[: fail_at + 1]
    assert log.read_text().splitlines() == expected
    assert completed.returncode == (0 if fail_at is None else 42)
    for harness in expected:
        assert re.search(rf"^{re.escape(harness)}: \d+\.\d+ seconds$", completed.stderr, re.M)


def test_pytest_matrix_runs_both_shards_without_cancelling_a_sibling() -> None:
    job = _jobs()["tests"]
    assert job["strategy"] == {"fail-fast": False, "matrix": {"shard": ["mcp-db", "other"]}}
    assert "matrix.shard" in job["name"]
    step = next(step for step in job["steps"] if step["name"] == "Test")
    assert step["env"]["PYTEST_SHARD"] == "${{ matrix.shard }}"
    assert step["run"] == 'just test-shard "$PYTEST_SHARD"'
