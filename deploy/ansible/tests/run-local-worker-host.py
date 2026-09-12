#!/usr/bin/env python3
"""Check the reusable worker role and the unchanged Ubuntu runner task contract."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

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
python_probe = "live_vm_host : Read the runner system Python version used by uv\tTAGS: []"
python_guard = next(
    index
    for index, task in enumerate(actual_tasks)
    if "Assert the runner host is Ubuntu/Debian" in task
)
require(
    actual_tasks[python_guard - 1] == python_probe,
    "runner system Python probe must immediately precede the Ubuntu guard",
)
actual_tasks.pop(python_guard - 1)
require(len(actual_tasks) == 308, f"runner listed {len(actual_tasks)} baseline tasks, expected 308")
for index, (expected, actual) in enumerate(zip(expected_tasks, actual_tasks, strict=True), 1):
    require(expected == actual, f"runner task {index} changed: {expected!r} -> {actual!r}")
print("ok runner: 308 ordered task names and tags match the updated baseline")

defaults = yaml.safe_load((ANSIBLE / "roles/local_worker_host/defaults/main.yml").read_text())
expected_packages = (TESTS / "fixtures/ubuntu-worker-packages-2391.txt").read_text().splitlines()
require(
    defaults["live_vm_host_packages"] == expected_packages, "Ubuntu worker package list changed"
)
print("ok runner: 19 Ubuntu worker packages match the pre-extraction baseline")

python_version_script = "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
system_python = subprocess.check_output(
    ["/usr/bin/python3", "-c", python_version_script],
    text=True,
).strip()
runner_tasks = yaml.safe_load((ANSIBLE / "roles/live_vm_host/tasks/main.yml").read_text())
python_tasks = runner_tasks[1:3]
require(
    [task["name"] for task in python_tasks]
    == [
        "Read the runner system Python version used by uv",
        "Assert the runner host is Ubuntu/Debian (system Python 3.14 + python3-guestfs, ADR-0387)",
    ],
    "runner Python guard moved",
)
opposite_minor = 13 if system_python == "3.14" else 14
with tempfile.TemporaryDirectory(prefix="kdive-python-guard-") as temp_dir:
    guard_probe = Path(temp_dir) / "guard.yml"
    guard_probe.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Probe runner system Python guard",
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "vars": {
                        "ansible_facts": {
                            "distribution": "Ubuntu",
                            "distribution_version": "26.04",
                            "python": {"version": {"major": 3, "minor": opposite_minor}},
                        }
                    },
                    "tasks": python_tasks,
                }
            ]
        )
    )
    guard = playbook(guard_probe, "--check")
require(
    (guard.returncode == 0) == (system_python == "3.14"),
    "runner Python guard followed Ansible facts instead of /usr/bin/python3",
)
print("ok runner: Python guard follows /usr/bin/python3 despite contrary Ansible facts")

jinja = Environment(undefined=StrictUndefined)
jinja.filters["basename"] = os.path.basename
guestfs_tasks = [
    task
    for task in runner_tasks
    if task["name"]
    in (
        "Symlink the libguestfs binding into the venv site-packages (no PyPI wheel exists)",
        "Symlink the target-native libguestfs binding into the authority venv",
        "Symlink the libguestfs binding into the lifecycle worker venv",
    )
]
require(len(guestfs_tasks) == 3, "expected three runner guestfs venv links")
for task in guestfs_tasks:
    destination = jinja.from_string(task["ansible.builtin.file"]["dest"]).render(
        live_vm_venv="/opt/kdive",
        live_vm_host_authority_runtime_install="/opt/kdive-provider-authority",
        live_vm_host_system_python_version={"stdout": "3.14"},
        ansible_python={"version": {"major": 3, "minor": 15}},
        item={"path": "/usr/lib/python3/dist-packages/guestfs.py"},
    )
    require(
        "/lib/python3.14/site-packages/guestfs.py" in destination,
        f"{task['name']} does not follow the validated system Python ABI",
    )
print("ok runner: all three guestfs venv links follow system Python, not Ansible Python")

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


def package_route(distribution: str, family: str, selected: str) -> None:
    facts = {
        "ansible_facts": {
            "distribution": distribution,
            "distribution_version": "probe",
            "os_family": family,
        },
        "local_worker_host_operator_user": operator,
    }
    result = playbook(
        probe, "--check", "--tags", "authority_prerequisites", "-e", json.dumps(facts)
    )
    require(
        "Require the standalone local-worker operator to exist" in result.stdout,
        f"{distribution} failed before package routing",
    )
    families = ("Debian", "RedHat", "Suse")
    for candidate in families:
        heading = (
            "TASK [local_worker_host : Install the kernel-debug toolchain + venv build deps "
            f"({candidate})]"
        )
        if families.index(candidate) > families.index(selected) and heading not in result.stdout:
            continue  # A missing native package manager can fail the selected task immediately.
        require(heading in result.stdout, f"{distribution} omitted {candidate} package task")
        section = result.stdout.split(heading, 1)[1].split("\nTASK [", 1)[0]
        if candidate == selected:
            require(
                "skipping: [localhost]" not in section,
                f"{distribution} skipped its {candidate} package task",
            )
        else:
            require(
                "skipping: [localhost]" in section,
                f"{distribution} entered the {candidate} package task",
            )


for distribution, family, selected in (
    ("Debian", "Debian", "Debian"),
    ("Fedora", "RedHat", "RedHat"),
    ("openSUSE Tumbleweed", "Suse", "Suse"),
):
    package_route(distribution, family, selected)
print("ok packages: check mode routes each supported family to only its package task")
