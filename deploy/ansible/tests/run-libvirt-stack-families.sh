#!/usr/bin/env bash
# Resolve the real role's package and daemon expressions without changing the host.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LIBVIRT_STACK_ROLE="$here/../roles/libvirt_stack"

python3 - <<'PY'
import os
from pathlib import Path

import yaml
from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment

role = Path(os.environ["LIBVIRT_STACK_ROLE"])
defaults = yaml.safe_load((role / "defaults/main.yml").read_text())
tasks = yaml.safe_load((role / "tasks/main.yml").read_text())
jinja = NativeEnvironment(undefined=StrictUndefined)


def require(condition, message):
    if not condition:
        raise SystemExit(f"libvirt_stack families: {message}")


def evaluate(expression, **facts):
    return jinja.compile_expression(expression)(**defaults, **facts)


def render(template, **facts):
    return jinja.from_string(template).render(**defaults, **facts)


def module_task(module):
    matches = [task for task in tasks if module in task]
    require(len(matches) == 1, f"expected one {module} task")
    return matches[0]


supported = ("Debian", "RedHat", "Suse")
require("ansible.builtin.assert" in tasks[0], "first task must refuse unsupported families")
guard = tasks[0]["ansible.builtin.assert"]
require(
    all(
        all(evaluate(condition, ansible_os_family=family) for condition in guard["that"])
        for family in supported
    ),
    "supported family refused",
)
require(
    not all(evaluate(condition, ansible_os_family="Other") for condition in guard["that"]),
    "unsupported family passed the first task",
)
message = render(guard["fail_msg"], ansible_os_family="Other")
require("Other" in message and "package set" in message, "unsupported-family error lacks context")

expected = {
    "Debian": [
        "libvirt-daemon-system", "libvirt-clients", "qemu-utils", "libguestfs-tools",
        "e2fsprogs", "virtinst", "gnutls-bin", "libseccomp2", "python3-libvirt",
        "python3-lxml",
    ],
    "RedHat": [
        "libvirt", "libvirt-client", "qemu-img", "libguestfs-tools-c", "e2fsprogs",
        "virt-install", "gnutls-utils", "libseccomp", "python3-libvirt", "python3-lxml",
    ],
    "Suse": [
        "libvirt-daemon-qemu", "libvirt-daemon-proxy", "libvirt-client", "qemu-tools",
        "guestfs-tools", "e2fsprogs", "virt-install", "gnutls", "libseccomp2",
        "python3-libvirt-python", "python3-lxml",
    ],
}
emulators = {
    "Debian": {"x86_64": "qemu-system-x86", "ppc64le": "qemu-system-ppc"},
    "RedHat": {"x86_64": "qemu-kvm", "ppc64le": "qemu-kvm"},
    "Suse": {"x86_64": "qemu-x86", "ppc64le": "qemu-ppc"},
}
modules = {
    "Debian": "ansible.builtin.apt",
    "RedHat": "ansible.builtin.dnf",
    "Suse": "community.general.zypper",
}
install = {family: module_task(module) for family, module in modules.items()}
for family in supported:
    for arch in ("x86_64", "ppc64le"):
        facts = {"ansible_os_family": family, "ansible_architecture": arch}
        require(
            [name for name, task in install.items() if evaluate(task["when"], **facts)]
            == [family],
            f"{family}/{arch} selected the wrong package task",
        )
        packages = render(install[family][modules[family]]["name"], **facts)
        require(
            packages == expected[family] + [emulators[family][arch]],
            f"{family}/{arch} package list differs: {packages}",
        )
        require(
            defaults["libvirt_stack_qemu_package_map"][family][arch] == emulators[family][arch],
            f"{family}/{arch} emulator map differs",
        )

modular = next(task for task in tasks if task.get("name", "").startswith("Switch to the modular"))
monolithic = next(task for task in tasks if task.get("name", "").startswith("Use the monolithic"))
for family in supported:
    facts = {"ansible_os_family": family}
    require(
        bool(evaluate(modular["when"], **facts)) == (family != "Debian"),
        f"{family} modular daemon route differs",
    )
    require(
        bool(evaluate(monolithic["when"], **facts)) == (family == "Debian"),
        f"{family} monolithic daemon route differs",
    )
require(len(modular["block"]) == 4, "modular daemon task count differs")
require(
    modular["block"][0]["loop"] == "{{ libvirt_stack_monolithic_units }}"
    and modular["block"][1]["loop"] == "{{ libvirt_stack_monolithic_units }}"
    and modular["block"][3]["loop"] == "{{ libvirt_stack_modular_sockets }}",
    "modular daemon unit loops differ",
)
unit_check = next(
    (task for task in modular["block"] if "ansible.builtin.command" in task), None
)
require(unit_check is not None, "monolithic unit state is not checked")
require(
    unit_check["ansible.builtin.command"]["argv"]
    == ["systemctl", "show", "--property=LoadState", "--value", "{{ item }}"],
    "monolithic unit check does not query systemd by name",
)
mask = next(task for task in modular["block"] if task.get("name", "").startswith("Disable and mask"))
for state, expected_units in (("not-found", []), ("masked", []), ("loaded", ["libvirtd.socket"])):
    states = {"results": [{"item": "libvirtd.socket", "stdout": state}]}
    require(
        render(mask["loop"], libvirt_stack_monolithic_unit_states=states) == expected_units,
        "monolithic mask did not follow systemd unit state",
    )
require(
    render(modular["block"][3]["loop"]) == [
        "virtqemud.socket", "virtnetworkd.socket", "virtstoraged.socket",
        "virtnodedevd.socket", "virtsecretd.socket", "virtproxyd.socket",
    ],
    "modular socket set differs",
)
require(
    monolithic["block"][1]["ansible.builtin.systemd_service"]["name"] == "libvirtd.socket",
    "Debian must keep libvirtd.socket",
)

group = module_task("ansible.builtin.user")
selected = render(
    group["ansible.builtin.user"]["name"],
    ansible_user="operator-test",
    ansible_user_id="root",
    ansible_env={"SUDO_USER": "operator-test"},
)
require(selected == "operator-test", "privileged facts selected root instead of the operator")
require(group["ansible.builtin.user"]["groups"] == ["kvm", "libvirt"], "operator groups differ")
require(
    group["ansible.builtin.user"]["append"] is True,
    "operator group membership is not additive",
)
print("libvirt_stack families: six package routes, daemon models, guard, and operator passed")
PY
