"""Contracts for installing repository-managed helpers into guest base images."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment

BUILD_ONE = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "ansible"
    / "roles"
    / "guest_base_image"
    / "tasks"
    / "build_one.yml"
)


def _relabel_command() -> str:
    tasks: list[dict[str, Any]] = yaml.safe_load(BUILD_ONE.read_text(encoding="utf-8"))
    task = next(
        task for task in tasks if "Build the virt-customize helper arguments" in task["name"]
    )
    expression = task["ansible.builtin.set_fact"]["guest_base_image_helper_args"]
    rendered = (
        Environment(autoescape=False)
        .from_string(expression)
        .render(
            guest_base_image_helper_args=[],
            guest_base_image_helper_dir="/tmp/helpers",
            item="kdive-helper",
        )
    )
    argv: list[str] = yaml.safe_load(rendered)
    run_commands = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--run-command"]
    return run_commands[-1]


def test_helper_install_succeeds_without_restorecon(tmp_path: Path) -> None:
    """Ubuntu catalog images do not install SELinux tooling."""
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()

    completed = subprocess.run(
        ["/bin/sh", "-c", _relabel_command()],
        env={**os.environ, "PATH": str(empty_bin)},
        check=False,
    )

    assert completed.returncode == 0


def test_helper_install_runs_restorecon_when_present(tmp_path: Path) -> None:
    """SELinux-capable images retain their helper-labeling behavior."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    invocation = tmp_path / "restorecon.args"
    restorecon = fake_bin / "restorecon"
    restorecon.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {invocation}\n", encoding="utf-8")
    restorecon.chmod(0o755)

    completed = subprocess.run(
        ["/bin/sh", "-c", _relabel_command()],
        env={**os.environ, "PATH": str(fake_bin)},
        check=False,
    )

    assert completed.returncode == 0
    assert invocation.read_text(encoding="utf-8").splitlines() == [
        "-v",
        "/usr/local/sbin/kdive-helper",
    ]


def test_helper_install_propagates_restorecon_failure(tmp_path: Path) -> None:
    """A broken SELinux relabel remains a failed image build."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    restorecon = fake_bin / "restorecon"
    restorecon.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
    restorecon.chmod(0o755)

    completed = subprocess.run(
        ["/bin/sh", "-c", _relabel_command()],
        env={**os.environ, "PATH": str(fake_bin)},
        check=False,
    )

    assert completed.returncode == 23
