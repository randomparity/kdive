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

pre_tasks = {task["name"]: task for task in play["pre_tasks"]}
require(
    "Require the operator account to exist before host mutation" in pre_tasks,
    "operator account must be validated before roles run",
)
checkout = pre_tasks["Require a complete local-libvirt source checkout before host mutation"]
require(
    "local_libvirt_host_checkout_entries.results" in str(checkout),
    "the checkout sentinel must be verified before roles run",
)
require(
    "pyproject.toml" in str(pre_tasks["Inspect required local-libvirt checkout entries"]),
    "the checkout sentinel must require the project manifest",
)
require(
    "uv.lock" in str(pre_tasks["Inspect required local-libvirt checkout entries"]),
    "the checkout sentinel must require the locked dependency set",
)
require(
    "install-live-worker-lifecycle.sh"
    in str(pre_tasks["Inspect required local-libvirt checkout entries"]),
    "the checkout sentinel must require the lifecycle installer",
)
require(
    "build-capture-bootstrap-manifest.py"
    in str(pre_tasks["Inspect required local-libvirt checkout entries"]),
    "the checkout sentinel must require the lifecycle manifest builder",
)
for required_payload in (
    "kdive-live-worker-gate",
    "kdive-live-worker-lifecycle",
    "kdive-live-worker@.service",
    "kdive-live-worker-lifecycle.socket",
    "kdive-live-worker-lifecycle@.service",
    "libvirtd-live.conf",
    "virtqemud-live.conf",
):
    require(
        required_payload in str(pre_tasks["Inspect required local-libvirt checkout entries"]),
        f"the checkout sentinel must require {required_payload}",
    )
require(
    "fixtures/local-libvirt" in str(pre_tasks["Inspect required local-libvirt checkout entries"]),
    "the checkout sentinel must require local-libvirt fixtures",
)
require(
    "console-ready_ppc64le.yaml"
    in str(pre_tasks["Inspect required local-libvirt checkout entries"]),
    "the checkout sentinel must require the ppc64le fixture profile",
)
require(
    "console-ready_x86_64.yaml"
    in str(pre_tasks["Inspect required local-libvirt checkout entries"]),
    "the checkout sentinel must require the x86_64 fixture profile",
)
require(
    "getent_passwd"
    in str(pre_tasks["Resolve the kernel source from the validated operator account"]),
    "the default kernel source must derive from the operator home",
)
git_worktree = pre_tasks["Verify the local-libvirt source is a Git worktree"]
require(
    git_worktree["ansible.builtin.command"]["argv"][-1] == "--is-inside-work-tree",
    "the checkout sentinel must probe the Git worktree",
)
require(
    "Require the local-libvirt source to be a Git worktree" in pre_tasks,
    "the checkout sentinel must reject non-Git source directories",
)
git_revision = pre_tasks["Verify the local-libvirt source has a revision"]
require(
    "HEAD^{commit}" in git_revision["ansible.builtin.command"]["argv"],
    "the checkout sentinel must require a resolvable Git revision",
)

tasks = {task["name"]: task for task in play["tasks"]}
task_names = [task["name"] for task in play["tasks"]]
sync_plan = tasks["Check whether the live dependency sync would change the project venv"]
require(
    sync_plan["ansible.builtin.command"]["argv"]
    == ["uv", "sync", "--locked", "--group", "live", "--dry-run"],
    "the live dependency sync plan must use the locked dependency set",
)
sync = tasks["Sync the project venv with the live dependencies"]
require(
    sync["ansible.builtin.command"]["argv"] == ["uv", "sync", "--locked", "--group", "live"],
    "project venv must be synced with the live group before it is used",
)
require(
    sync["become_user"] == "{{ local_libvirt_host_operator_user }}",
    "the project venv must be owned by the operator",
)
require(
    "local_libvirt_host_live_sync_plan" in str(sync["changed_when"]),
    "the project venv sync must report its actual change receipt",
)
lifecycle = tasks["Install the fixed live-worker lifecycle contract"]
require(
    task_names.index(sync["name"]) < task_names.index(lifecycle["name"]),
    "the lifecycle installer must run after the project venv sync",
)
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
rootfs = tasks["Create the local rootfs publication directory"]["ansible.builtin.file"]
require(rootfs["mode"] == "2770", "the rootfs publication directory must be group-setgid")
require(
    rootfs["owner"] == "{{ local_libvirt_host_operator_user }}",
    "the rootfs publication directory must be owned by the operator",
)
print(
    "local-libvirt-host: preflight, localhost role composition, locked live sync, DSN stdin, "
    "and guestfs ABI handling pass"
)
