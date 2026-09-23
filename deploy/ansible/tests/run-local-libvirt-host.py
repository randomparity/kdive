#!/usr/bin/env python3
"""Check the localhost local-libvirt playbook contract without applying it."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
ANSIBLE = ROOT / "deploy/ansible"
PLAYBOOK = ANSIBLE / "playbooks/local-libvirt-host.yml"
INVENTORY = ANSIBLE / "inventory/hosts.yml"
SYSTEM_INTERPRETER = "/usr/bin/python3"


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

# The lifecycle installer below (deploy/systemd/install-live-worker-lifecycle.sh) runs as root
# through this play's `become: true` and resolves `uv` with a bare `command -v uv`; a host
# provisioned per docs/operating/install.md installs uv to ~/.local/bin, which root's sudo
# secure_path does not list, so the installer fails without a manual `uv` placement (#2665). Roles
# always run before a play's own `tasks:`, so local_worker_host installing a root-resolvable uv
# (reusing live_vm_host's mechanism rather than duplicating it) is what makes the lifecycle
# installer below resolvable with no manual step -- assert the role actually carries that task.
LOCAL_WORKER_HOST = ANSIBLE / "roles/local_worker_host"
require(
    "import_tasks: uv.yml" in (LOCAL_WORKER_HOST / "tasks/main.yml").read_text(),
    "local_worker_host must install a root-resolvable uv (see #2665) before this play's own "
    "tasks run, or the lifecycle installer below fails on a host with only a user-local uv",
)
uv_task = yaml.safe_load((LOCAL_WORKER_HOST / "tasks/uv.yml").read_text())[0]
uv_pip = uv_task.get("ansible.builtin.pip", {})
require(
    uv_pip.get("name") == "uv" and uv_pip.get("state") == "present",
    "the shared uv install task local_worker_host imports no longer installs uv via pip",
)
uv_defaults = yaml.safe_load((LOCAL_WORKER_HOST / "defaults/main.yml").read_text())
require(
    uv_defaults.get("live_vm_host_uv_bin") == "/usr/local/bin/uv",
    "live_vm_host_uv_bin must resolve to the path pip installs and secure_path lists",
)
# live_vm_host imports the same task file (tasks_from: uv.yml) rather than a second pip install,
# so this play's fix and the runner's stay one mechanism, not a duplicate that can drift (#2665).
require(
    "tasks_from: uv.yml" in (ANSIBLE / "roles/live_vm_host/tasks/main.yml").read_text(),
    "live_vm_host must reuse the same shared uv install task local_worker_host uses",
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
# grpcio (core dependency via opentelemetry-exporter-otlp-proto-grpc) has no ppc64le wheel and
# its vendored BoringSSL has no ppc64le target, so this operator-owned sync source-builds it
# against the system OpenSSL/zlib; an interactively exported flag never reaches this unattended
# task (#2666). Inert on an arch where grpcio installs from a wheel.
grpc_env = {
    "GRPC_PYTHON_BUILD_SYSTEM_OPENSSL": "1",
    "GRPC_PYTHON_BUILD_SYSTEM_ZLIB": "1",
}
require(
    sync_plan.get("environment") == grpc_env,
    "the live dependency sync dry-run must pin the grpcio system-OpenSSL/zlib build flags",
)
require(
    sync.get("environment") == grpc_env,
    "the live dependency sync must pin the grpcio system-OpenSSL/zlib build flags",
)
lifecycle = tasks["Install the fixed live-worker lifecycle contract"]
require(
    task_names.index(sync["name"]) < task_names.index(lifecycle["name"]),
    "the lifecycle installer must run after the project venv sync",
)
require(lifecycle["no_log"] is True, "lifecycle DSN task must not log input")
# The DSN reaches the installer on stdin, so the module arguments carry a live credential and the
# task result has to stay censored. Censoring the failure with it made an installer exit 127
# undiagnosable (#2506), so the task registers its result, defers the failure, and a follow-up
# task outside no_log reports the return code and the installer's own stderr.
require(
    lifecycle.get("failed_when") is False,
    "the censored lifecycle installer must defer its failure to an uncensored task",
)
require(
    lifecycle.get("register") == "local_libvirt_host_lifecycle_install",
    "the censored lifecycle installer must register its result",
)
lifecycle_failure = tasks["Report a failed live-worker lifecycle installation"]
require(
    task_names.index(lifecycle["name"]) + 1 == task_names.index(lifecycle_failure["name"]),
    "the lifecycle installer failure must be reported before any later task runs",
)
require(
    "no_log" not in lifecycle_failure,
    "the lifecycle installer failure report must not be censored",
)
require(
    "local_libvirt_host_lifecycle_install.rc" in str(lifecycle_failure["when"]),
    "the lifecycle installer failure must be detected by its return code",
)
for required_evidence in ("rc", "stderr"):
    require(
        required_evidence in str(lifecycle_failure["ansible.builtin.fail"]["msg"]),
        f"the lifecycle installer failure report must carry the installer {required_evidence}",
    )
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


# The pin is a play var, not an inventory host var. A play var outranks the implicit-localhost
# interpreter, which is what binds modules to whichever Python launched ansible-playbook -- under
# the recipe, a `uv run --with ansible-core` environment with no lxml for community.libvirt.
require(
    play["vars"].get("ansible_python_interpreter") == SYSTEM_INTERPRETER,
    "the play must pin ansible_python_interpreter to "
    f"{SYSTEM_INTERPRETER} in its own vars, not rely on the inventory or on discovery",
)


def declared_hosts(group: dict) -> set[str]:
    """Every host named anywhere in the inventory, not just at the top level."""
    named = set(group.get("hosts") or {})
    for child in (group.get("children") or {}).values():
        named |= declared_hosts(child or {})
    return named


inventory = yaml.safe_load(INVENTORY.read_text())
require(
    "localhost" not in declared_hosts(inventory["all"]),
    "hosts.yml must not declare localhost anywhere; the localhost plays that do not pin their own "
    "interpreter (playbooks/pki.yml, most of deploy/ansible/tests/) would fall back to "
    "ansible-core interpreter discovery instead of the launching environment",
)

print(
    "local-libvirt-host: preflight, localhost role composition, root-resolvable uv, locked live "
    "sync, DSN stdin, guestfs ABI handling, and the play-scoped system-interpreter pin pass"
)
