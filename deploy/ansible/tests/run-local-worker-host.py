#!/usr/bin/env python3
"""Check the reusable worker role and the unchanged Ubuntu runner task contract."""

import json
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
ANSIBLE = ROOT / "deploy/ansible"
TESTS = ANSIBLE / "tests"
ENV = os.environ.copy()
ENV["ANSIBLE_CONFIG"] = str(ANSIBLE / "ansible.cfg")
ENV["ANSIBLE_ROLES_PATH"] = str(ANSIBLE / "roles")


def playbook(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ansible-playbook", str(path), "-i", "localhost,", *args],
        cwd=ROOT,
        env=ENV,
        capture_output=True,
        text=True,
        check=False,
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"local-worker-host regression: {message}")


def tasks(output: str, *, include_tags: bool = False) -> list[str]:
    lines = [
        line.strip().replace("local_worker_host : ", "live_vm_host : ")
        for line in output.splitlines()
        if "\tTAGS:" in line and not line.lstrip().startswith("play #")
    ]
    return lines if include_tags else [line.split("\tTAGS:", 1)[0] for line in lines]


runner = playbook(ANSIBLE / "playbooks/runner.yml", "--list-tasks")
require(runner.returncode == 0, "runner task listing failed")
expected_tasks = (TESTS / "fixtures/runner-tasks-2391.txt").read_text().splitlines()
actual_tasks = tasks(runner.stdout, include_tags=True)
require(len(actual_tasks) == 306, f"runner listed {len(actual_tasks)} tasks, expected 306")
for index, (expected, actual) in enumerate(zip(expected_tasks, actual_tasks, strict=True), 1):
    require(expected == actual, f"runner task {index} changed: {expected!r} -> {actual!r}")
print("ok runner: 306 ordered task names and tags match the pre-extraction baseline")

defaults = yaml.safe_load((ANSIBLE / "roles/local_worker_host/defaults/main.yml").read_text())
expected_packages = (TESTS / "fixtures/ubuntu-worker-packages-2391.txt").read_text().splitlines()
require(
    defaults["live_vm_host_packages"] == expected_packages, "Ubuntu worker package list changed"
)
print("ok runner: 19 Ubuntu worker packages match the pre-extraction baseline")

probe = TESTS / "local_worker_host.yml"
syntax = playbook(probe, "--syntax-check")
require(syntax.returncode == 0, "standalone role syntax check failed")
listed = playbook(probe, "--list-tasks")
require(listed.returncode == 0, "standalone role task listing failed")
standalone_tasks = tasks(listed.stdout)
require(
    "live_vm_host : Create the isolated live-worker accounts" in standalone_tasks,
    "standalone fixed-worker account task is absent",
)
require(
    "live_vm_host : Create group-writable provider data directories" in standalone_tasks,
    "standalone shared-directory task is absent",
)
for forbidden in (
    "Docker Engine",
    "runner service account",
    "fixture catalog",
    "authority service",
):
    require(
        not any(forbidden in task for task in standalone_tasks),
        f"standalone role includes runner-only {forbidden}",
    )
print("ok standalone: account and data tasks parse without runner-only tasks")


def preflight(distribution: str, family: str, user: str) -> subprocess.CompletedProcess[str]:
    facts = {
        "ansible_facts": {
            "distribution": distribution,
            "distribution_version": "probe",
            "os_family": family,
        },
        "local_worker_host_operator_user": user,
    }
    return playbook(probe, "--tags", "always", "-e", json.dumps(facts))


operator = subprocess.check_output(["id", "-un"], text=True).strip()
for distribution, family in (
    ("Debian", "Debian"),
    ("Ubuntu", "Debian"),
    ("Fedora", "RedHat"),
    ("openSUSE Tumbleweed", "Suse"),
):
    result = preflight(distribution, family, operator)
    require(result.returncode == 0, f"{distribution} standalone preflight failed")
for distribution, family in (("openSUSE Leap", "Suse"), ("SLES", "Suse"), ("Rocky", "RedHat")):
    result = preflight(distribution, family, operator)
    require(result.returncode != 0, f"{distribution} passed the unsupported-host preflight")
    require(
        "Require a supported standalone local-worker distribution" in result.stdout,
        "wrong refusal task",
    )
    require(
        "Install the kernel-debug toolchain" not in result.stdout,
        "unsupported host reached packages",
    )
for user in ("", "kdive-nonexistent-2391"):
    result = preflight("Fedora", "RedHat", user)
    require(result.returncode != 0, f"invalid operator {user!r} passed preflight")
    refusal = (
        "Require a named standalone local-worker operator"
        if user == ""
        else "Require the standalone local-worker operator to exist"
    )
    require(refusal in result.stdout, f"invalid operator {user!r} failed at the wrong task")
    require(
        "Install the kernel-debug toolchain" not in result.stdout,
        "invalid operator reached packages",
    )
print("ok preflight: named distributions and operator paths fail before mutation")
