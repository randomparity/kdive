#!/usr/bin/env python3
"""Exercise the real remote egress tasks against read-only command boundaries."""

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
ANSIBLE = ROOT / "deploy/ansible"
TASKS = ANSIBLE / "roles/libvirt_pool_net/tasks/egress_preflight.yml"
FORWARD = "-P FORWARD DROP\n-A FORWARD -j DOCKER-USER\n-A FORWARD -j DOCKER-FORWARD"
EMPTY = "-N DOCKER-USER"


def run_case(name, forward=FORWARD, user=EMPTY, *, rc=0, user_rc=0, missing=False, failure=None):
    with tempfile.TemporaryDirectory(prefix="kdive-egress-test-") as directory:
        work = Path(directory)
        calls = work / "calls"
        if not missing:
            tool = work / "iptables"
            tool.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}\n"
                f'case "$*" in\n'
                f"'-S FORWARD') printf '%s\\n' {shlex.quote(forward)}; exit {rc};;\n"
                f"'-S DOCKER-USER') printf '%s\\n' {shlex.quote(user)}; exit {user_rc};;\n"
                "*) exit 90;;\nesac\n"
            )
            tool.chmod(0o755)
        play = work / "play.yml"
        play.write_text(
            yaml.safe_dump(
                [
                    {
                        "hosts": "localhost",
                        "connection": "local",
                        "gather_facts": False,
                        "environment": {"PATH": str(work)},
                        "vars": {
                            "libvirt_network": "selected-network",
                            "ansible_python_interpreter": sys.executable,
                        },
                        "tasks": [
                            {"ansible.builtin.import_tasks": str(TASKS)},
                            {"ansible.builtin.debug": {"msg": "egress-preflight-completed"}},
                        ],
                    }
                ]
            )
        )
        log = work / "result.log"
        with log.open("w") as output:
            result = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", str(play)],
                env={**os.environ, "ANSIBLE_CONFIG": str(ANSIBLE / "ansible.cfg")},
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        text = log.read_text()
        assert (result.returncode != 0) == bool(failure), (name, text)
        if failure:
            assert failure in text, (name, text)
            assert "egress-preflight-completed" not in text, (name, text)
        else:
            assert "egress-preflight-completed" in text, (name, text)
        commands = calls.read_text().splitlines() if calls.exists() else []
        assert all(command in ["-S FORWARD", "-S DOCKER-USER"] for command in commands)
        if missing:
            assert not commands
        print(f"ok {name}")


run_case("empty-user-chain", failure="Docker FORWARD DROP")
run_case(
    "terminal-return", user=EMPTY + "\n-A DOCKER-USER -j RETURN", failure="Docker FORWARD DROP"
)
run_case("custom-policy", user=EMPTY + "\n-A DOCKER-USER -i selected-bridge -j ACCEPT")
run_case("additional-forward-rule", forward=FORWARD + "\n-A FORWARD -i selected-bridge -j ACCEPT")
run_case("accept-policy", forward=FORWARD.replace("FORWARD DROP", "FORWARD ACCEPT"))
run_case("non-docker", forward="-P FORWARD DROP")
run_case("inspection-error", rc=4, failure="Read IPv4 forwarding rules")
run_case("user-inspection-error", user_rc=1, failure="Read Docker user forwarding rules")
run_case("tool-absent", missing=True)

plays = yaml.safe_load((ANSIBLE / "site.yml").read_text())
preflight = next(
    i
    for i, play in enumerate(plays)
    if any(
        task.get("ansible.builtin.import_role", {}).get("tasks_from") == "egress_preflight"
        for task in play.get("tasks", [])
    )
)
authority = next(
    i
    for i, play in enumerate(plays)
    if any(
        isinstance(role, dict) and role.get("role") == "provider_authority_host"
        for role in play.get("roles", [])
    )
)
facts = next(i for i, play in enumerate(plays) if "remote_libvirt_facts" in play.get("roles", []))
assert authority < preflight < facts
assert "egress_preflight" not in (ANSIBLE / "roles/libvirt_pool_net/tasks/main.yml").read_text()
print("ok remote-only post-firewall pre-inventory ordering")
