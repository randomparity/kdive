#!/usr/bin/env python3
"""Check the localhost playbook and execute its path tasks in temporary fixtures."""

import os
import shutil
import subprocess
import tempfile
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

# community.libvirt virt_pool/virt_net ignore `autostart` in a task that also sets `state`
# (#3203), so each object libvirt_pool_net activates needs its own autostart-only task.
pool_net_tasks = yaml.safe_load((ANSIBLE / "roles/libvirt_pool_net/tasks/main.yml").read_text())
pcap_pool_tasks = yaml.safe_load(
    (ANSIBLE / "roles/libvirt_pool_net/tasks/pcap_pool.yml").read_text()
)
require(
    {
        "name": "Provision the traffic-capture pcap pool",
        "ansible.builtin.import_tasks": "pcap_pool.yml",
    }
    in pool_net_tasks,
    "libvirt_pool_net must import the pcap pool tasks (#3316)",
)
libvirt_args = [
    (module, task[module])
    for task in pool_net_tasks + pcap_pool_tasks
    for module in ("community.libvirt.virt_pool", "community.libvirt.virt_net")
    if module in task
]
require(
    not any("state" in args and "autostart" in args for _, args in libvirt_args),
    "a libvirt_pool_net task combines state and autostart; the module ignores autostart there",
)
for module, name in (
    ("community.libvirt.virt_pool", "default"),
    ("community.libvirt.virt_pool", "{{ libvirt_pool_net_boot_artifact_pool_name }}"),
    ("community.libvirt.virt_pool", "{{ libvirt_pool_net_pcap_pool_name }}"),
    ("community.libvirt.virt_net", "{{ libvirt_network }}"),
):
    require(
        (module, {"name": name, "state": "active"}) in libvirt_args
        and (module, {"name": name, "autostart": True}) in libvirt_args,
        f"{module} {name} must be activated and autostarted by separate tasks",
    )

# The confined QEMU creates each capture pcap in this pool (#3316, ADR-0432). A pool build resets a
# directory to libvirt's 0711 default unless the pool XML carries the permissions, and only an
# svirt_image_t label (SELinux) or the libvirt-qemu.d drop-in (AppArmor) lets the domain create it.
pcap_by_module: dict[str, list[dict]] = {}
for task in pcap_pool_tasks:
    for module in task:
        pcap_by_module.setdefault(module, []).append(task)
pcap_define = next(
    t["community.libvirt.virt_pool"]
    for t in pcap_by_module["community.libvirt.virt_pool"]
    if t["community.libvirt.virt_pool"].get("command") == "define"
)
for permission in (
    "<mode>{{ libvirt_pool_net_pcap_pool_mode }}</mode>",
    "<owner>{{ libvirt_pool_net_pcap_pool_passwd[1] }}</owner>",
    "<group>0</group>",
):
    require(permission in pcap_define["xml"], f"pcap pool XML must carry {permission}")


def gates(task: dict) -> list[str]:
    when = task.get("when", [])
    return [when] if isinstance(when, str) else list(when)


# Every enforcing host needs the label (libvirt_stack installs sefcontext's bindings on both SELinux
# families), so the rule and its relabel carry exactly that one gate.
SELINUX_GATES = ["libvirt_pool_net_selinux_mode.stdout | default('') == 'Enforcing'"]
pcap_fcontext = pcap_by_module["community.general.sefcontext"]
require(
    len(pcap_fcontext) == 1
    and pcap_fcontext[0]["community.general.sefcontext"]["setype"] == "svirt_image_t"
    and gates(pcap_fcontext[0]) == SELINUX_GATES,
    "pcap pool fcontext must be svirt_image_t and gated on Enforcing",
)
pcap_relabel = [
    t
    for t in pcap_by_module["ansible.builtin.command"]
    if "restorecon" in t["ansible.builtin.command"]["argv"]
]
require(
    len(pcap_relabel) == 1
    and "-F" not in pcap_relabel[0]["ansible.builtin.command"]["argv"]
    and gates(pcap_relabel[0]) == SELINUX_GATES,
    "pcap pool relabel must be one plain restorecon, never -F (ADR-0640), gated like the rule",
)
# Every AppArmor task runs only where libvirt's abstraction exists; ungated, the include assert
# fails on every host without AppArmor.
APPARMOR_GATE = ["libvirt_pool_net_apparmor_abstraction.stat.exists"]
APPARMOR_TASKS = {
    "Read libvirt's AppArmor QEMU abstraction",
    "Require the abstraction to include its libvirt-qemu.d drop-ins",
    "Create the libvirt-qemu.d AppArmor drop-in directory",
    "Allow the confined QEMU to write capture pcaps",
}
apparmor_tasks = [t for t in pcap_pool_tasks if t["name"] in APPARMOR_TASKS]
require(
    len(apparmor_tasks) == 4 and all(gates(t) == APPARMOR_GATE for t in apparmor_tasks),
    "the abstraction read, include assert, drop-in directory and drop-in must be AppArmor-gated",
)
pcap_dropin = [
    t["ansible.builtin.copy"]
    for t in pcap_by_module["ansible.builtin.copy"]
    if "libvirt-qemu.d" in t["ansible.builtin.copy"]["dest"]
]
require(
    len(pcap_dropin) == 1
    and "{{ libvirt_pool_net_pcap_pool_target }}/kdive-pcap-*.pcap rw,"
    in pcap_dropin[0]["content"],
    "pcap pool must install the libvirt-qemu.d AppArmor drop-in for kdive-pcap-*.pcap",
)
pcap_by_name = {t["name"]: t for t in pcap_pool_tasks}
selinux_probe = pcap_by_name["Read the SELinux mode for the pcap pool label"]
require(
    selinux_probe["ansible.builtin.command"]["argv"] == ["getenforce"]
    and selinux_probe.get("failed_when") is False
    and selinux_probe.get("changed_when") is False,
    "the pcap getenforce probe must not fail on a host without SELinux tools",
)
include_assert = pcap_by_name["Require the abstraction to include its libvirt-qemu.d drop-ins"]
require(
    any(
        "'abstractions/libvirt-qemu.d'" in str(c)
        for c in include_assert["ansible.builtin.assert"]["that"]
    ),
    "the AppArmor probe must look for the libvirt-qemu.d include, not the abstraction's own name",
)
QEMU_OWNER = "libvirt_pool_net_pcap_pool_owner_by_family[ansible_os_family]"
require(
    QEMU_OWNER
    in pcap_by_name["Create the traffic-capture pcap pool directory"]["ansible.builtin.file"][
        "owner"
    ]
    and any(
        "pw_name" in str(c) and QEMU_OWNER in str(c)
        for c in pcap_by_name["Assert the pcap pool directory is owned by the QEMU runtime user"][
            "ansible.builtin.assert"
        ]["that"]
    ),
    "the pcap pool directory must be created and asserted as the family's QEMU runtime user",
)
pcap_directory = pcap_by_name["Create the traffic-capture pcap pool directory"][
    "ansible.builtin.file"
]
require(
    pcap_directory["group"] == "root"
    and pcap_directory["mode"] == "{{ libvirt_pool_net_pcap_pool_mode }}",
    "the pcap pool directory must be group root (not the QEMU user's kvm group) and use the mode",
)
ownership_assert = pcap_by_name["Assert the pcap pool directory is owned by the QEMU runtime user"][
    "ansible.builtin.assert"
]["that"]
require(
    "libvirt_pool_net_pcap_pool_stat.stat.gr_name == 'root'" in ownership_assert
    and "libvirt_pool_net_pcap_pool_stat.stat.mode == libvirt_pool_net_pcap_pool_mode"
    in ownership_assert,
    "the pcap pool ownership assert must check the root group and the mode",
)
access_check = pcap_by_name[
    "Require the QEMU runtime user to reach and write the pcap pool directory"
]["ansible.builtin.command"]["argv"]
require(
    access_check.count("{{ libvirt_pool_net_pcap_pool_target }}") == 2
    and access_check[access_check.index("-w") : access_check.index("-w") + 2]
    == ["-w", "{{ libvirt_pool_net_pcap_pool_target }}"],
    "the QEMU-user access check must test the pcap pool target for write and search",
)
target_check = pcap_by_name["Require the pcap pool to target the provisioned directory"]
require(
    any(
        "libvirt_pool_net_pcap_pool_target" in str(c) and "get_xml" in str(c)
        for c in target_check["ansible.builtin.assert"]["that"]
    ),
    "an existing pcap pool definition must be checked against the provisioned target",
)
# A parent the QEMU user cannot traverse defeats the leaf's ownership (review of #3316).
pool_defaults = yaml.safe_load((ANSIBLE / "roles/libvirt_pool_net/defaults/main.yml").read_text())
require(
    not pool_defaults["libvirt_pool_net_pcap_pool_target"].startswith(
        pool_defaults["libvirt_pool_net_boot_artifact_pool_target"].rsplit("/", 1)[0] + "/"
    ),
    "the pcap pool must not sit under the root:libvirt 0770 boot-artifact parent",
)
require(
    any(
        t["ansible.builtin.command"]["argv"][:2] == ["runuser", "-u"]
        and "-w" in t["ansible.builtin.command"]["argv"]
        for t in pcap_by_module["ansible.builtin.command"]
    ),
    "the role must prove the QEMU runtime user can write the pcap pool directory",
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
uv_tasks_by_name = {
    task["name"]: task for task in yaml.safe_load((LOCAL_WORKER_HOST / "tasks/uv.yml").read_text())
}
uv_pip = uv_tasks_by_name.get(
    "Install uv system-wide (the venv builder; not in the debug toolchain)", {}
).get("ansible.builtin.pip", {})
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

# ADR-0640: on an SELinux-enforcing RedHat host a confined domain can only write and map images
# labeled svirt_image_t. This play owns the two parent fcontext rules since install-host.sh became
# a wrapper around it (#2779); build-image.sh owns the nested rootfs/local rule, and a second
# writer here would reorder the last-match-wins local rules the ADR measured.
require(
    "policycoreutils-python-utils" in uv_defaults["local_worker_host_packages_redhat"],
    "local_worker_host must install semanage (policycoreutils-python-utils) on the RedHat family",
)
role_main = yaml.safe_load((LOCAL_WORKER_HOST / "tasks/main.yml").read_text())
role_imports = [task.get("ansible.builtin.import_tasks") for task in role_main]
LABELS = "selinux_image_labels.yml"
require(
    LABELS in role_imports
    and role_imports.index("shared_directories.yml") < role_imports.index(LABELS),
    "the svirt_image_t labels must be applied after the shared directories exist",
)
require(
    role_main[role_imports.index(LABELS)].get("when") == "ansible_facts['os_family'] == 'RedHat'",
    "the svirt_image_t labels must be scoped to the RedHat family",
)
require(
    role_imports.index("packages_redhat.yml") < role_imports.index(LABELS),
    "semanage's Python bindings must be installed before the fcontext rules are written",
)
label_tasks = yaml.safe_load((LOCAL_WORKER_HOST / "tasks" / LABELS).read_text())
label_tasks_by_name = {task["name"]: task for task in label_tasks}
mode_probe = label_tasks[0]
require(
    mode_probe.get("ansible.builtin.command", {}).get("argv") == ["getenforce"]
    and mode_probe.get("changed_when") is False
    and mode_probe.get("failed_when") is False,
    "the SELinux mode must be read from getenforce without reporting a change, and a host "
    "without getenforce must skip the labels rather than fail",
)
enforcing = "local_worker_host_selinux_mode.stdout | default('') == 'Enforcing'"
rules = label_tasks_by_name["Install the kdive image svirt_image_t fcontext rules"]
require(rules.get("when") == enforcing, "the fcontext rules must be guarded on enforcing SELinux")
require(
    rules["community.general.sefcontext"]
    == {
        "target": "{{ item }}(/.*)?",
        "setype": "svirt_image_t",
        "state": "present",
    },
    "the fcontext rules must set svirt_image_t on each directory and everything under it",
)
require(
    uv_defaults["local_worker_host_svirt_image_directories"]
    == [
        "/var/lib/kdive/host-dump",
        "/var/lib/kdive/rootfs",
        "/var/lib/kdive/install",
        "/var/lib/kdive/build/images",
        "/var/lib/kdive/rootfs-cache",
        "/var/lib/kdive/rootfs-uploads",
        "/var/lib/kdive/pcap",
    ],
    "the play must own the ADR-0640 roots, the ADR-0764 build/images subtree, the uploaded-"
    "rootfs staging root qemu reads in place and the pcap root qemu writes, not rootfs/local",
)
require(
    "/var/lib/kdive/build/images" in uv_defaults["live_vm_host_worker_shared_directories"],
    "the image workspace must use the existing shared-directory creation policy",
)
require(
    "/var/lib/kdive/rootfs-cache" in uv_defaults["live_vm_host_worker_shared_directories"],
    "the persistent published-image cache must use the shared-directory creation policy",
)
require(
    "/var/lib/kdive/rootfs-uploads" in uv_defaults["live_vm_host_worker_shared_directories"],
    "the uploaded-rootfs staging root must exist for the workers, who cannot create it",
)
relabel = label_tasks_by_name["Relabel the kdive image directories"]
require(relabel.get("when") == enforcing, "the relabel must be guarded on enforcing SELinux")
require(
    relabel["ansible.builtin.command"]["argv"] == ["restorecon", "-R", "-v", "{{ item }}"],
    "the relabel must be a recursive, verbose restorecon without -F (which would strip the MCS "
    "categories of a running domain's images)",
)
require(
    "Relabeled" in str(relabel.get("changed_when")),
    "the relabel must report a change only when restorecon relabeled a file",
)
require(
    [task["name"] for task in label_tasks].index(rules["name"])
    < [task["name"] for task in label_tasks].index(relabel["name"]),
    "restorecon must run after the fcontext rules are written",
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
    "Resolve the kernel source from the validated operator account" not in pre_tasks,
    "host preparation must not resolve a kernel tree; workers never read one (#3193)",
)
require(
    "local_libvirt_host_kernel_source" not in play["vars"],
    "host preparation must not take a kernel tree; workers never read one (#3193)",
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
require(
    task_names.index(lifecycle_failure["name"]) < task_names.index(mismatch["name"]),
    "the mismatch message relies on the lifecycle installer's guestfs failure having run first",
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
traversal = tasks["Make every checkout ancestor traversable by workers"]
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
    "local-libvirt-host: preflight, localhost role composition, root-resolvable uv, svirt_image_t "
    "labels, locked live sync, DSN stdin, guestfs ABI handling, and the play-scoped "
    "system-interpreter pin pass"
)


# Execute the production tasks, including Ansible's absent-stat shape and conditional
# evaluation. Only temporary paths need chmod; /tmp and / already allow traversal.
tmp_mode = Path("/tmp").stat().st_mode
require(tmp_mode & 0o001 != 0, "/tmp must already allow traversal")
with tempfile.TemporaryDirectory(prefix="kdive-traversal-", dir="/tmp") as temporary:
    fixture = Path(temporary)
    # Contain chmod to this fixture: sticky /tmp ends in 't', so the production
    # permission probe requests a redundant o+x there even though it is traversable.
    shim = fixture / "bin"
    shim.mkdir()
    chmod = shim / "chmod"
    chmod.write_text(
        "#!/bin/bash\nset -euo pipefail\n"
        '[[ "$1" == o+x && "$2" == -- ]]\n'
        'if [[ "$3" == /tmp ]]; then exit 0; fi\n'
        f'[[ "$3" == {temporary}/* || "$3" == {temporary} ]]\n'
        f'exec {shutil.which("chmod")} "$@"\n'
    )
    chmod.chmod(0o700)
    environment = {"PATH": f"{shim}:{os.environ['PATH']}"}
    source = fixture / "checkout"
    source.mkdir(mode=0o700)
    traversal_play = fixture / "traversal.yml"
    traversal_play.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Traversal fixture",
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "environment": environment,
                    "vars": {"local_libvirt_host_source": str(source)},
                    "tasks": [tasks["Inspect the checkout root"], traversal],
                }
            ]
        )
    )
    for _ in range(2):
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(traversal_play)],
            capture_output=True,
            text=True,
            check=False,
        )
        require(result.returncode == 0, result.stdout + result.stderr)
    require(Path("/tmp").stat().st_mode == tmp_mode, "/tmp permissions changed")
    for directory in (fixture, source):
        require(directory.stat().st_mode & 0o777 == 0o701, "ancestor traversal mode differs")

    # Missing and incomplete required checkouts must still fail before host mutation.
    for checkout_source in (fixture / "missing-checkout", source):
        checkout_play = fixture / "checkout.yml"
        checkout_play.write_text(
            yaml.safe_dump(
                [
                    {
                        "name": "Required checkout fixture",
                        "hosts": "localhost",
                        "connection": "local",
                        "gather_facts": False,
                        "vars": {"local_libvirt_host_source": str(checkout_source)},
                        "tasks": play["pre_tasks"][:5],
                    }
                ]
            )
        )
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(checkout_play)],
            capture_output=True,
            text=True,
            check=False,
        )
        require(result.returncode == 2, "invalid required checkout was accepted: " + result.stdout)
        expected = (
            "Require a complete local-libvirt source checkout before host mutation"
            if checkout_source == source
            else "Require a real local-libvirt source checkout"
        )
        require(f"TASK [{expected}]" in result.stdout, "checkout failed before its required guard")
print("local-libvirt-host: actual traversal fixtures and required-checkout rejection pass")
