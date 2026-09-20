#!/usr/bin/env python3
"""Check the reusable worker role and the unchanged Ubuntu runner task contract."""

import json
import os
import re
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
require(len(actual_tasks) == 320, f"runner listed {len(actual_tasks)} baseline tasks, expected 320")
for index, (expected, actual) in enumerate(zip(expected_tasks, actual_tasks, strict=True), 1):
    require(expected == actual, f"runner task {index} changed: {expected!r} -> {actual!r}")
print("ok runner: 320 ordered task names and tags match the updated baseline")

defaults = yaml.safe_load((ANSIBLE / "roles/local_worker_host/defaults/main.yml").read_text())
expected_packages = (TESTS / "fixtures/ubuntu-worker-packages-2391.txt").read_text().splitlines()
require(
    defaults["live_vm_host_packages"] == expected_packages, "Ubuntu worker package list changed"
)
print("ok runner: 20 Ubuntu worker packages match the pre-extraction baseline")

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
    ("Rocky", "RedHat"),
    ("openSUSE Tumbleweed", "Suse"),
    ("SLES", "Suse"),
):
    result = preflight(distribution, family, operator)
    require(result.returncode == 0, f"{distribution} standalone preflight failed")
for distribution, family in (("openSUSE Leap", "Suse"),):
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
    ("Rocky", "RedHat", "RedHat"),
    ("openSUSE Tumbleweed", "Suse", "Suse"),
    ("SLES", "Suse", "Suse"),
):
    package_route(distribution, family, selected)
print("ok packages: check mode routes each supported family to only its package task")

# The container runtime is declared for one distribution per family, not for the family.
CONTAINER_RUNTIME = {"RedHat": "Fedora", "Suse": "Tumbleweed"}


def container_section(output: str, name: str, context: str) -> str:
    heading = f"TASK [local_worker_host : {name}]"
    require(heading in output, f"{context} never reached: {name}")
    return output.split(heading, 1)[1].split("\nTASK [", 1)[0]


def container_runtime_gate(distribution: str, family: str, *, declared: bool) -> None:
    """Only Fedora and openSUSE Tumbleweed may enter the container-runtime installs (#2505).

    `preflight.yml` admits RHEL, Rocky and AlmaLinux beside Fedora and SLES beside Tumbleweed,
    and none of those four package the engine or the compose plugin their sibling does. The
    distribution gate, not the family route, is what keeps dnf/zypper from being handed a
    package the host cannot resolve, so it is the gate worth proving.

    `--start-at-task` skips the family toolchain install, which check mode cannot run as a
    non-root user. The engine and compose modules themselves DO run: whichever one the gates
    admit ends in a module failure here (dnf without root, zypper absent entirely), which is
    why only skip-versus-entered is asserted and the playbook return code is not.

    The engine's second gate reads a registered stat of /usr/bin/docker, so both of its arms
    are driven by injecting that result as an extra-var, which outranks the register. Reading
    the runner's real /usr/bin instead would leave the no-provider arm — the state #2505 is
    about — asserting nothing on any runner that happens to have docker installed.
    """
    label = CONTAINER_RUNTIME[family]
    probe_name = f"Look for an existing container engine before installing one ({family})"
    engine_name = f"Install the {label} container engine"
    compose_name = f"Install the {label} compose plugin for the on-box stack and testcontainers"
    for provider_exists in (False, True):
        facts = {
            "ansible_facts": {
                "distribution": distribution,
                "distribution_version": "probe",
                "os_family": family,
            },
            "local_worker_host_operator_user": operator,
            f"local_worker_host_docker_provider_{family.lower()}": {
                "stat": {"exists": provider_exists}
            },
        }
        result = playbook(
            probe,
            "--check",
            "--tags",
            "authority_prerequisites",
            "--start-at-task",
            probe_name,
            "-e",
            json.dumps(facts),
        )
        state = f"{distribution} (provider present: {provider_exists})"
        engine = container_section(result.stdout, engine_name, state)
        engine_skipped = "skipping: [localhost]" in engine
        if not declared:
            # With no provider injected, the distribution gate is the only thing that can skip
            # these, so a pass here cannot be an accident of the runner's own /usr/bin.
            require(engine_skipped, f"{state} entered the {label} engine install")
            compose = container_section(result.stdout, compose_name, state)
            require(
                "skipping: [localhost]" in compose,
                f"{state} entered the {label} compose plugin install",
            )
            continue
        require(
            engine_skipped == provider_exists,
            f"{state} got the wrong {label} engine arm: skipped={engine_skipped}",
        )
        if not provider_exists:
            continue  # the engine entered and failed the module, so the play stops here
        # A host that already has a provider keeps it, and the compose plugin — the only source
        # of the `docker compose` subcommand stack-services.sh runs — must still be installed.
        compose = container_section(result.stdout, compose_name, state)
        require(
            "skipping: [localhost]" not in compose,
            f"{state} skipped the {label} compose plugin",
        )


for distribution, family, declared in (
    ("Fedora", "RedHat", True),
    ("Rocky", "RedHat", False),
    ("RedHat", "RedHat", False),
    ("AlmaLinux", "RedHat", False),
    ("openSUSE Tumbleweed", "Suse", True),
    ("SLES", "Suse", False),
):
    container_runtime_gate(distribution, family, declared=declared)
print("ok container runtime: only Fedora and Tumbleweed enter the engine and compose installs")


def container_runtime_order() -> None:
    """Probe, then engine, then compose plugin, in both families.

    Installing the plugin first would resolve its `(engine or podman)` dependency by pulling
    the CLI package that owns /usr/bin/docker, and the probe would then suppress the engine
    install on that run and every later one — leaving a host with a CLI, a plugin and no
    daemon. The order is the contract; a silent reorder must not survive (#2505).
    """
    for family, label in CONTAINER_RUNTIME.items():
        ordered = (
            f"Look for an existing container engine before installing one ({family})",
            f"Install the {label} container engine",
            f"Install the {label} compose plugin for the on-box stack and testcontainers",
        )
        positions = []
        for name in ordered:
            require(name in listed.stdout, f"{label} container task missing from listing: {name}")
            positions.append(listed.stdout.index(name))
        require(
            positions == sorted(positions),
            f"{label} container tasks are out of order: probe, engine, then compose plugin",
        )


container_runtime_order()
print("ok container runtime: the engine installs before the compose plugin in both families")


def boot_kernel_guard(distribution: str, family: str) -> None:
    """RedHat and Suse ship /boot kernels world-readable; relabelling there would NARROW
    them, so the block must skip on every family except Debian (ADR-0222, #2479).

    Only the skip arm is driven here: on a Debian-facts run the block would reach the kvm
    membership assertion, which needs the fixed worker accounts this checkout does not have.
    """
    facts = {
        "ansible_facts": {
            "distribution": distribution,
            "distribution_version": "probe",
            "os_family": family,
        },
        "local_worker_host_operator_user": operator,
    }
    result = playbook(probe, "--check", "--tags", "boot_kernels", "-e", json.dumps(facts))
    for heading in (
        (
            "TASK [local_worker_host : Find the host kernels under /boot "
            "(vmlinuz-* x86_64, vmlinux-* ppc64le)]"
        ),
        "TASK [local_worker_host : Install the kernel-upgrade hook that re-applies the relabel]",
    ):
        require(heading in result.stdout, f"{distribution} omitted {heading}")
        section = result.stdout.split(heading, 1)[1].split("\nTASK [", 1)[0]
        require(
            "skipping: [localhost]" in section,
            f"{distribution} entered the Debian-only host-kernel relabel",
        )


for distribution, family in (
    ("Fedora", "RedHat"),
    ("Rocky", "RedHat"),
    ("openSUSE Tumbleweed", "Suse"),
    ("SLES", "Suse"),
):
    boot_kernel_guard(distribution, family)
print("ok boot kernels: the /boot relabel and its upgrade hook skip every non-Debian family")


# The hook re-applies the relabel from outside Ansible, so its constants are the ones that must
# hold and no play asserts them. 0644 is the specific value boot_kernels.yml:2-11 forbids: Fedora
# ships /boot world-readable, and widening a Debian host to match is the regression the
# Debian-family guard exists to prevent. Proving the hook relabels a real upgraded kernel needs a
# Debian-family host and a privileged package install, which no repository gate can stage (#2567).
BOOT_HOOK = ANSIBLE / "roles/local_worker_host/files/kernel-postinst-kvm-readable"
# Comments are stripped first: the hook's own comment explains why 0644 is forbidden, and a
# check that reads it cannot tell that sentence from a chmod reaching the value.
hook_code = "\n".join(
    line for line in BOOT_HOOK.read_text().splitlines() if not line.lstrip().startswith("#")
)
# Every mode the hook applies is matched, not just the presence of one good literal: a check that
# only looked for "chmod 0640" stays green beside an added `chmod o+r`, which is exactly the
# widening boot_kernels.yml:11 forbids. Double-quoted strings are dropped first so the hook's own
# diagnostic messages, which name both commands, cannot register as calls.
hook_calls = re.sub(r'"[^"]*"', "", hook_code)
require(
    re.findall(r"\bchmod\s+(\S+)", hook_calls) == ["0640"],
    "the kernel-upgrade hook applies a mode other than exactly 0640",
)
require(
    re.findall(r"\bchgrp\s+(\S+)", hook_calls) == ["kvm"],
    "the kernel-upgrade hook applies a group other than exactly kvm",
)
for pattern in ("/boot/vmlinuz-*", "/boot/vmlinux-*"):
    require(
        pattern in hook_code,
        f"the kernel-upgrade hook stopped covering {pattern}",
    )
# One shared hook, imported by both roles, never a second copy: a duplicated relabel is how this
# defect came to exist at two sites (local_worker_host and live_vm_host), and a duplicated hook
# would repeat that. Assert the install task is defined exactly once across the whole role tree.
HOOK_TASK = "Install the kernel-upgrade hook that re-applies the relabel"
definitions = sorted(
    path.relative_to(ANSIBLE).as_posix()
    for path in (ANSIBLE / "roles").rglob("tasks/*.yml")
    if f"name: {HOOK_TASK}" in path.read_text()
)
require(
    definitions == ["roles/local_worker_host/tasks/boot_kernel_hook.yml"],
    f"the kernel-upgrade hook install must be defined exactly once, found {definitions}",
)
copies = sorted(
    path.relative_to(ANSIBLE).as_posix()
    for path in (ANSIBLE / "roles").rglob("files/*")
    if path.name == BOOT_HOOK.name
)
require(
    copies == [f"roles/local_worker_host/files/{BOOT_HOOK.name}"],
    f"the kernel-upgrade hook file must be shipped exactly once, found {copies}",
)
# Both call sites reach that one definition, so the runner host is covered too (#2567).
require(
    "tasks_from: boot_kernel_hook.yml"
    in (ANSIBLE / "roles/live_vm_host/tasks/main.yml").read_text(),
    "live_vm_host no longer imports the shared kernel-upgrade hook, so the runner is uncovered",
)
hook_task = yaml.safe_load(
    (ANSIBLE / "roles/local_worker_host/tasks/boot_kernel_hook.yml").read_text()
)[0]
# Its own guard, not the caller's: boot_kernels.yml's block still skips non-Debian without this,
# so only the live_vm_host import — which has no surrounding guard — depends on the task carrying
# one. Removing it therefore leaves every other arm of this harness green.
require(
    hook_task.get("when") == "ansible_facts['os_family'] == 'Debian'",
    "the shared kernel-upgrade hook lost its own Debian-family guard",
)
hook_install = hook_task.get("ansible.builtin.copy", {})
# src is asserted too: the non-Debian probes all skip this task, so a src naming a file that does
# not exist would never be resolved by any check-mode run and every other assertion stays green.
require(
    hook_install.get("src") == BOOT_HOOK.name,
    "the kernel-upgrade hook task no longer copies the shipped hook file",
)
require(
    hook_install.get("dest") == "/etc/kernel/postinst.d/kdive-kvm-readable",
    "the kernel-upgrade hook is no longer installed into /etc/kernel/postinst.d",
)
require(
    hook_install.get("mode") == "0755",
    "the kernel-upgrade hook is no longer installed executable",
)
print("ok boot kernels: the upgrade hook applies 0640 root:kvm to both kernel patterns")


CONTAINER_TASKS = "roles/local_worker_host/tasks/container_runtime.yml"
DAEMON_PROBE = "Look for a packaged container-engine service unit"
DAEMON_ENABLE = "Enable and start the container-engine daemon"
DAEMON_LOOKUP = "Look up the container-engine socket grant's target account"
DAEMON_EXCLUDE = "Require the container-engine socket grant to name a non-worker account"
DAEMON_GRANT = "Add the named operator account to the container-engine socket group"


def container_daemon_facts(*, unit_exists: bool) -> dict[str, object]:
    return {
        "ansible_facts": {
            "distribution": "Fedora",
            "distribution_version": "probe",
            "os_family": "RedHat",
        },
        "local_worker_host_operator_user": operator,
        "local_worker_host_engine_unit": {"stat": {"exists": unit_exists}},
    }


def container_daemon_gate(*, unit_exists: bool) -> None:
    """Both mutating tasks follow the packaged-unit probe, never the runner's own state.

    The register is injected as an extra-var, which outranks it, so the skip arm proves the gate
    even on a runner that has docker installed. The skip arm is also the podman-docker case: on a
    Fedora host where podman-docker already owns /usr/bin/docker, packages_redhat.yml skips the
    engine install and no docker.service exists, and that must stay a clean skip rather than a
    failure (#2557, ADR-0663).

    Each task is read from its own invocation started at that task. In the entered arm the modules
    themselves run — check mode, non-root, against whatever systemd and group state the runner has
    — so an enable that failed there would otherwise stop the play before the grant is reached. The
    engine-install gate above starts at its probe for the same reason.
    """
    facts = container_daemon_facts(unit_exists=unit_exists)
    state = f"unit present: {unit_exists}"
    for name in (DAEMON_ENABLE, DAEMON_GRANT):
        result = playbook(
            probe,
            "--check",
            "--tags",
            "container_runtime",
            "--start-at-task",
            name,
            "-e",
            json.dumps(facts),
        )
        section = container_section(result.stdout, name, state)
        skipped = "skipping: [localhost]" in section
        require(
            skipped != unit_exists,
            f"{state} got the wrong arm of {name!r}: skipped={skipped}",
        )


for engine_unit_exists in (False, True):
    container_daemon_gate(unit_exists=engine_unit_exists)
print("ok container daemon: the packaged-unit probe gates both the enable and the grant")


def container_daemon_guard(*, operator_user: str, refused: bool) -> None:
    """The guard runs, and refuses before the enable — not merely appears above it in the listing.

    The structural assertions in container_daemon_grant_target prove the clauses are written. They
    cannot prove the guard fires: an ignore_errors, a failed_when: false, a block/rescue wrapper or
    a call site shadowing live_vm_host_worker_accounts with an empty list would all keep the text
    and lose the refusal. Starting at the guard is what executes it (#2557, ADR-0575).
    """
    facts = container_daemon_facts(unit_exists=True)
    facts["local_worker_host_operator_user"] = operator_user
    result = playbook(
        probe,
        "--check",
        "--tags",
        "container_runtime",
        "--start-at-task",
        DAEMON_LOOKUP,
        "-e",
        json.dumps(facts),
    )
    state = f"operator {operator_user!r}"
    section = container_section(result.stdout, DAEMON_EXCLUDE, state)
    guard_failed = "fatal: [localhost]" in section
    require(
        guard_failed == refused,
        f"{state} got the wrong guard outcome: refused={guard_failed}",
    )
    reached_enable = f"TASK [local_worker_host : {DAEMON_ENABLE}]" in result.stdout
    require(
        reached_enable != refused,
        f"{state} {'reached' if reached_enable else 'did not reach'} the enable after the guard",
    )


for guard_user, guard_refused in (
    ("kdive-worker-1", True),
    ("", True),
    ("kdive-nonexistent-2557", True),
    (operator, False),
):
    container_daemon_guard(operator_user=guard_user, refused=guard_refused)
print("ok container daemon: the guard refuses worker, empty and absent accounts before the enable")


def container_daemon_grant_target() -> None:
    """The socket group goes to the operator variable, in the file and at the runner call site.

    Socket-group membership is root-equivalent and ADR-0575 keeps the fixed worker slot accounts
    out of it, so a grant naming live_vm_host_worker_accounts would dissolve that boundary
    silently. The runner call site is the one place the operator variable is rebound, and a lost
    vars key there would fall back to the role default of "" rather than to a refusal, because
    import_role with tasks_from does not run preflight.yml.
    """
    source = (ANSIBLE / CONTAINER_TASKS).read_text()
    by_name = {task["name"]: task for task in yaml.safe_load(source)}
    for name in (DAEMON_PROBE, DAEMON_LOOKUP, DAEMON_EXCLUDE, DAEMON_ENABLE, DAEMON_GRANT):
        require(name in by_name, f"container_runtime.yml has no task named {name!r}")
    grant = by_name[DAEMON_GRANT].get("ansible.builtin.user", {})
    require(
        grant.get("name") == "{{ local_worker_host_operator_user }}",
        "the container-engine socket grant does not target the operator variable",
    )
    require(grant.get("append") is True, "the container-engine socket grant replaces memberships")
    enable = by_name[DAEMON_ENABLE].get("ansible.builtin.systemd_service", {})
    require(
        enable.get("enabled") is True and enable.get("state") == "started",
        "the container-engine daemon task does not both enable and start the service",
    )
    exclusion = by_name[DAEMON_EXCLUDE].get("ansible.builtin.assert", {}).get("that", [])
    require(
        any("not in live_vm_host_worker_accounts" in str(c) for c in exclusion),
        "nothing keeps the socket grant off the fixed worker accounts (ADR-0575)",
    )
    require(
        any("local_worker_host_operator_user | length > 0" in str(c) for c in exclusion),
        "the socket grant does not refuse an empty operator account",
    )
    require(
        any("getent_passwd" in str(c) for c in exclusion),
        "the socket grant does not refuse an account the host does not have",
    )
    register = by_name[DAEMON_PROBE].get("register")
    require(bool(register), "the container-engine probe registers no result to gate on")
    for name in (DAEMON_ENABLE, DAEMON_GRANT):
        require(
            by_name[name].get("when") == f"{register}.stat.exists",
            f"{name!r} is not gated on the packaged-unit probe's own register {register!r}",
        )
    known = set(defaults) | {"local_worker_host_engine_unit"}
    used = set(re.findall(r"\{\{\s*(local_worker_host_[a-z_]+)", source))
    require(
        used <= known,
        f"container_runtime.yml uses undeclared role variables: {sorted(used - known)}",
    )
    runner_tasks = yaml.safe_load((ANSIBLE / "roles/live_vm_host/tasks/main.yml").read_text())
    call = next(
        task
        for task in runner_tasks
        if task.get("ansible.builtin.import_role", {}).get("tasks_from") == "container_runtime.yml"
    )
    require(
        call.get("vars", {}).get("local_worker_host_operator_user") == "{{ github_runner_user }}",
        "the runner call site does not rebind the operator variable to the runner account",
    )


container_daemon_grant_target()
print("ok container daemon: the socket grant targets only the named operator, at both sites")


def container_daemon_order() -> None:
    """Probe, enable, grant — all after the compose plugin that resolves the engine."""
    ordered = (
        "Install the Tumbleweed compose plugin for the on-box stack and testcontainers",
        DAEMON_PROBE,
        DAEMON_LOOKUP,
        DAEMON_EXCLUDE,
        DAEMON_ENABLE,
        DAEMON_GRANT,
    )
    positions = []
    for name in ordered:
        require(name in listed.stdout, f"container daemon task missing from listing: {name}")
        positions.append(listed.stdout.index(name))
    require(
        positions == sorted(positions),
        "container daemon tasks are out of order: probe, lookup, guard, enable, then grant",
    )


container_daemon_order()
print("ok container daemon: the enable and grant follow the engine install")
