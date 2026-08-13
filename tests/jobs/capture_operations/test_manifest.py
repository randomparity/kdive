"""Build/install and deployment guards for capture bootstrap attestation (ADR-0558)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[3]
_SCRIPT = _ROOT / "scripts/build-capture-bootstrap-manifest.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args], text=True, capture_output=True, check=False
    )


def test_builder_records_real_interpreter_arch_and_absolute_dependency_closure(
    tmp_path: Path,
) -> None:
    output = tmp_path / "manifest.json"
    result = _run(
        "build",
        "--interpreter",
        sys.executable,
        "--source-root",
        str(_ROOT / "src"),
        "--output",
        str(output),
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(output.read_text())
    assert manifest["interpreter"] == str(Path(sys.executable).resolve())
    assert manifest["architecture"] in {"x86_64", "ppc64le"}
    assert all(Path(entry["path"]).is_absolute() for entry in manifest["files"])
    assert any(entry["kind"] == "elf-interpreter" for entry in manifest["files"])
    assert any(entry["kind"] == "bootstrap-python" for entry in manifest["files"])
    assert output.read_bytes().endswith(b"\n")


def test_builder_refresh_is_atomic_and_idempotent(tmp_path: Path) -> None:
    output = tmp_path / "manifest.json"
    arguments = (
        "build",
        "--interpreter",
        sys.executable,
        "--source-root",
        str(_ROOT / "src"),
        "--output",
        str(output),
    )
    first = _run(*arguments)
    first_inode = output.stat().st_ino
    second = _run(*arguments)

    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "changed"
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "unchanged"
    assert output.stat().st_ino == first_inode


def test_verify_rejects_wrong_interpreter_and_import_trace_drift(tmp_path: Path) -> None:
    output = tmp_path / "manifest.json"
    built = _run(
        "build",
        "--interpreter",
        sys.executable,
        "--source-root",
        str(_ROOT / "src"),
        "--output",
        str(output),
    )
    assert built.returncode == 0, built.stderr
    wrong = _run(
        "verify",
        "--manifest",
        str(output),
        "--interpreter",
        "/bin/true",
        "--source-root",
        str(_ROOT / "src"),
    )
    assert wrong.returncode != 0
    assert "interpreter" in wrong.stderr

    payload = json.loads(output.read_text())
    payload["bootstrap_modules"].append("tenant.module")
    output.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    drift = _run(
        "verify",
        "--manifest",
        str(output),
        "--interpreter",
        sys.executable,
        "--source-root",
        str(_ROOT / "src"),
    )
    assert drift.returncode != 0
    assert "import trace" in drift.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="unprivileged refusal requires a non-root test uid")
def test_install_refuses_unprivileged_user(tmp_path: Path) -> None:
    staged = tmp_path / "staged.json"
    staged.write_text("{}\n")
    result = _run(
        "install", "--staged", str(staged), "--destination", str(tmp_path / "installed.json")
    )
    assert result.returncode != 0
    assert "root" in result.stderr


def test_docker_and_ansible_generate_after_final_interpreter() -> None:
    dockerfile = (_ROOT / "Dockerfile").read_text()
    build = dockerfile.index("build-capture-bootstrap-manifest.py build")
    final = dockerfile.index("FROM python:3.14.6-slim-bookworm", dockerfile.index("AS builder") + 1)
    user = dockerfile.index("USER kdive")
    assert final < build < user
    assert "build-capture-bootstrap-manifest.py install" in dockerfile[build:user]

    tasks = (_ROOT / "deploy/ansible/roles/libvirt_stack/tasks/main.yml").read_text()
    assert "Build capture bootstrap manifest with target-native interpreter" in tasks
    assert "Install root-owned capture bootstrap manifest" in tasks
    assert tasks.index("Build capture bootstrap manifest") < tasks.index(
        "Install root-owned capture bootstrap manifest"
    )


def test_package_initializer_is_inert_under_isolated_python() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "import sys, kdive; print('kdive.version' in sys.modules, "
                "'__version__' in kdive.__dict__)"
            ),
        ],
        cwd=_ROOT,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False False"
