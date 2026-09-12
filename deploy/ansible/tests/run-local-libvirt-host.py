#!/usr/bin/env python3
"""Check the localhost local-libvirt playbook contract without applying it."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
PLAYBOOK = ROOT / "deploy/ansible/playbooks/local-libvirt-host.yml"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"local-libvirt-host regression: {message}")


play = yaml.safe_load(PLAYBOOK.read_text())[0]
require(play["hosts"] == "localhost", "playbook must target localhost")
require(play["connection"] == "local", "playbook must use the local connection")
require(play["become"] is True, "playbook must install host prerequisites as root")
require(
    play["roles"] == ["libvirt_stack", "libvirt_pool_net", "local_worker_host"],
    "role order differs",
)

tasks = {task["name"]: task for task in play["tasks"]}
sync = tasks["Sync the project venv with the live dependencies"]
require(
    sync["ansible.builtin.command"]["argv"] == ["uv", "sync", "--group", "live"],
    "project venv must be synced with the live group before it is used",
)
lifecycle = tasks["Install the fixed live-worker lifecycle contract"]
require(lifecycle["no_log"] is True, "lifecycle DSN task must not log input")
command = lifecycle["ansible.builtin.command"]
require(
    "stdin" in command and "stdin_add_newline" in command,
    "lifecycle DSN must use stdin",
)
require(
    "local_libvirt_host_witness_dsn" not in str(command["argv"]),
    "DSN must not be an argument",
)
require(
    "KDIVE_LIFECYCLE_WITNESS_DATABASE_URL" in str(play["vars"]["local_libvirt_host_witness_dsn"]),
    "playbook must read the DSN from its environment",
)

mismatch = tasks["Explain why the system guestfs binding cannot be shared"]
require(
    "when" in mismatch and "msg" in mismatch["ansible.builtin.debug"],
    "guestfs mismatch must report",
)
link = tasks["Link the system guestfs binding into the project venv"]
require(link["ansible.builtin.file"]["state"] == "link", "guestfs binding must be linked")
require("when" in link, "guestfs binding must be ABI guarded")
require(
    "Require the system guestfs binding files" in tasks,
    "guestfs discovery must reject an incomplete binding",
)
require(
    "Verify the linked guestfs binding imports from the project venv" in tasks,
    "guestfs binding must be imported after linking",
)
traversal = tasks["Make every existing checkout and kernel ancestor traversable by workers"]
require("while" in traversal["ansible.builtin.shell"], "traversal must walk ancestors")
print(
    "local-libvirt-host: localhost target, role composition, DSN stdin, "
    "and guestfs ABI handling pass"
)
