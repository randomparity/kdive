#!/usr/bin/env python3
"""RHEL-family remote images serve GRUB on the serial console (issue #3118).

A remote domain has no video device, so a GRUB left on gfxterm hangs the guest before the
kernel prints anything. This reads the REAL role task and checks that it rewrites the GRUB
terminal for exactly the RHEL-family catalog distros, regenerates the BIOS config, and runs
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
guard = Environment().compile_expression(task["when"][0])
for distro, wanted in (
    ("rocky", True),
    ("fedora", True),
    ("ubuntu", False),
    ("opensuse-leap", False),
    ("bare", False),
):
    if bool(guard(image={"distro": distro})) != wanted:
        fail(f"the task runs={not wanted} for {distro}")
print("guest image console: RHEL-family images serve GRUB on serial before staging")
