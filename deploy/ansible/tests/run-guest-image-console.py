#!/usr/bin/env python3
"""The x86_64 Rocky remote image serves GRUB on the serial console (issue #3118).

A remote domain has no video device, so a GRUB left on gfxterm hangs the guest before the
kernel prints anything. This reads the REAL role task and checks that it rewrites the GRUB
terminal for exactly the proven arm (x86_64 Rocky), regenerates the BIOS config, and runs
before the image is staged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from jinja2 import Environment

ROLE = Path(__file__).resolve().parent.parent / "roles/guest_base_image/tasks/build_one.yml"
NAME = "Serve the GRUB terminal on the serial console for {{ image.name }}"
STAGE = "Stage the finished image into the (root-owned) pool for {{ image.name }}"


def fail(message: str) -> None:
    sys.exit(f"guest image console: {message}")


tasks = yaml.safe_load(ROLE.read_text())
names = [task.get("name") for task in tasks]
if NAME not in names:
    fail("the serial-console GRUB task is missing")
task = tasks[names.index(NAME)]
if names.index(NAME) > names.index(STAGE):
    fail("the GRUB console must be set before the image is staged")
script = task["ansible.builtin.command"]["argv"][-1]
for needle in ('GRUB_TERMINAL="serial console"', "grub2-mkconfig -o /boot/grub2/grub.cfg"):
    if needle not in script:
        fail(f"the task does not carry {needle!r}")
env = Environment()
guards = [env.compile_expression(cond) for cond in task["when"][:2]]
for distro, arch, wanted in (
    ("rocky", "x86_64", True),
    ("rocky", "ppc64le", False),
    ("fedora", "x86_64", False),
    ("ubuntu", "x86_64", False),
    ("opensuse-leap", "x86_64", False),
    ("bare", "x86_64", False),
):
    runs = all(guard(image={"distro": distro}, ansible_architecture=arch) for guard in guards)
    if runs != wanted:
        fail(f"the task runs={runs} for {distro}/{arch}")
print("guest image console: the x86_64 Rocky image serves GRUB on serial before staging")
