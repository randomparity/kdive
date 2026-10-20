#!/usr/bin/env python3
"""Exercise the image role's serial-console script and selection guards (#3236).

Guest paths are redirected into a temporary directory and grub2-mkconfig is harmless.
This proves script behavior, not an Ansible image build or a real headless boot.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from jinja2 import Environment

ROLE = Path(__file__).resolve().parent.parent / "roles/guest_base_image/tasks/build_one.yml"
NAME = "Serve the GRUB terminal on the serial console for {{ image.name }}"
STAGE = "Stage the finished image into the (root-owned) pool for {{ image.name }}"


def fail(message: str) -> None:
    sys.exit(f"guest image console: {message}")


def check_script(script: str) -> None:
    with tempfile.TemporaryDirectory(prefix="kdive-console-") as directory:
        root = Path(directory)
        defaults, config = root / "defaults", root / "grub.cfg"
        stub = root / "grub2-mkconfig"
        stub.write_text(
            '#!/bin/sh\nset -eu\n[ "$#" = 2 ] && [ "$1" = -o ]\n'
            '[ "$2" = "$CONSOLE_TEST_CONFIG" ]\n'
            'cp "$CONSOLE_TEST_DEFAULTS" "$2"\nexit "${CONSOLE_TEST_EXIT:-0}"\n'
        )
        stub.chmod(0o755)
        script = script.replace("/etc/default/grub", str(defaults)).replace(
            "/boot/grub2/grub.cfg", str(config)
        )
        environment = dict(
            os.environ,
            PATH=f"{root}:{os.environ['PATH']}",
            CONSOLE_TEST_DEFAULTS=str(defaults),
            CONSOLE_TEST_CONFIG=str(config),
        )
        preserved = '# keep comment\nGRUB_TIMEOUT=5\nGRUB_CMDLINE_LINUX="console=ttyS0"\n'
        expected = preserved + (
            'GRUB_TERMINAL="serial console"\nGRUB_SERIAL_COMMAND="serial --speed=115200"\n'
        )
        conflicts = (
            'GRUB_TERMINAL="gfxterm"\nGRUB_TERMINAL_INPUT="console"\n'
            'GRUB_TERMINAL_OUTPUT="gfxterm"\nGRUB_SERIAL_COMMAND="serial --speed=9600"\n'
            'GRUB_TERMINAL_OUTPUT="console"\n'
        )
        for initial in (preserved, preserved + conflicts):
            defaults.write_text(initial)
            for _ in range(2):
                config.unlink(missing_ok=True)
                subprocess.run(["/bin/sh", "-c", script], env=environment, check=True)
                if defaults.read_text() != expected or config.read_text() != expected:
                    fail("rewrite, unrelated content, idempotence or regeneration order changed")
        failed = subprocess.run(
            ["/bin/sh", "-c", script], env=dict(environment, CONSOLE_TEST_EXIT="23"), check=False
        )
        if failed.returncode != 23:
            fail("grub2-mkconfig failure must propagate before staging")


tasks = yaml.safe_load(ROLE.read_text())
names = [task.get("name") for task in tasks]
if NAME not in names:
    fail("the serial-console GRUB task is missing")
task = tasks[names.index(NAME)]
if names.index(NAME) > names.index(STAGE):
    fail("the GRUB console must be set before the image is staged")
env = Environment()
env.filters["bool"] = bool  # The matrix supplies native bools, as the role's facts do.
prereqs = yaml.safe_load(
    (ROLE.parent.parent.parent / "guest_image_prereqs/tasks/main.yml").read_text()
)
for distribution, family in (
    ("Fedora", "RedHat"),
    ("Rocky", "RedHat"),
    ("RedHat", "RedHat"),
    ("CentOS", "RedHat"),
    ("Debian", "Debian"),
    ("Ubuntu", "Debian"),
    ("Archlinux", "Archlinux"),
):
    facts = {"ansible_distribution": distribution, "ansible_os_family": family}
    packages = []
    for prerequisite in prereqs:
        if "ansible.builtin.dnf" not in prerequisite:
            continue
        conditions = prerequisite.get("when", [])
        if isinstance(conditions, str):
            conditions = [conditions]
        if not all(env.compile_expression(condition)(**facts) for condition in conditions):
            continue
        selected = prerequisite["ansible.builtin.dnf"]["name"]
        if isinstance(selected, str):
            selected = env.compile_expression(selected.strip()[2:-2].strip())(**facts)
        assert isinstance(selected, list), "prerequisite packages must render as a list"
        packages.extend(selected)
    expected_packages = ["dnf", "libguestfs-tools"] if family == "RedHat" else []
    if distribution == "Fedora":
        expected_packages.append("guestfs-tools")
    if packages != expected_packages:
        fail(f"prerequisite packages changed for {distribution}: {packages}")
print("guest image console: Fedora tool prerequisites and other package routes passed")
guards = [env.compile_expression(cond) for cond in task["when"]]
force_expression = tasks[0]["ansible.builtin.set_fact"]["guest_base_image_force"]
effective_force = env.compile_expression(force_expression.strip()[2:-2].strip())
families = ("rocky", "fedora", "ubuntu", "debian", "opensuse-leap", "bare")
for distro, arch, staged, global_force, image_force in itertools.product(
    families, ("x86_64", "ppc64le", "aarch64"), (False, True), (False, True), (None, False, True)
):
    image: dict[str, str | bool] = {"distro": distro}
    if image_force is not None:
        image["force"] = image_force
    force = effective_force(image=image, force_image_rebuild=global_force)
    runs = all(
        guard(
            image=image,
            ansible_architecture=arch,
            guest_base_image_staged={"stat": {"exists": staged}},
            guest_base_image_force=force,
        )
        for guard in guards
    )
    wanted = (
        distro in ("rocky", "fedora")
        and arch == "x86_64"
        and (not staged or global_force or bool(image_force))
    )
    if runs != wanted:
        fail(
            f"selection changed: {distro}/{arch}, staged={staged}, "
            f"force={global_force}/{image_force}"
        )
if sys.platform == "linux":
    check_script(task["ansible.builtin.command"]["argv"][-1])
    print("guest image console: extracted Linux serial script passed")
else:
    print("guest image console: extracted Linux script NOT RUN on this host; Linux CI required")
print("guest image console: Fedora/Rocky x86_64 guards and pre-stage ordering passed")
