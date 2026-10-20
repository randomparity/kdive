"""Offline contracts for both inventory provisioners; no host actions are executed."""

import configparser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

ROLES = Path("deploy/ansible/roles")
SHARED = ROLES / "provider_authority_host/tasks/inventory_observer.yml"


def tasks():
    return yaml.safe_load(SHARED.read_text())


def flatten(items):
    for item in items:
        yield item
        for key in ("block", "rescue", "always"):
            yield from flatten(item.get(key, []))


@pytest.mark.parametrize("role", ["provider_authority_host", "live_vm_host"])
def test_role_parity(role):
    defaults = yaml.safe_load((ROLES / role / "defaults/main.yml").read_text())
    opt = role + (
        "_inventory_observer_enabled"
        if role == "provider_authority_host"
        else "_authority_inventory_observer_enabled"
    )
    assert defaults[opt] is False
    hooks = [
        t
        for t in flatten(yaml.safe_load((ROLES / role / "tasks/main.yml").read_text()))
        if str(t.get("ansible.builtin.include_tasks", "")).endswith("inventory_observer.yml")
    ]
    assert len(hooks) == 1 and "when" not in hooks[0]
    variables = hooks[0]["vars"]
    env = Environment(undefined=StrictUndefined)
    values = (
        yaml.safe_load((ROLES / "local_worker_host/defaults/main.yml").read_text())
        | defaults
        | {role + "_source_root": "/candidate", "live_vm_venv": "/candidate"}
    )
    for variable in variables.values():
        env.from_string(str(variable)).render(values)
    assert variables["inventory_observer_enabled"] == "{{ " + opt + " }}"
    names = [t["name"] for t in tasks()]
    assert names.index("Withdraw the inventory listener before convergence") < names.index(
        "Converge the enabled inventory boundary"
    )


def test_install_order_and_failure():
    top = tasks()
    flat = list(flatten(top))
    by_name = {t["name"]: t for t in flat}
    enabled = by_name["Converge the enabled inventory boundary"]
    assert enabled["when"] == "inventory_observer_enabled | bool"
    assert any(
        t.get("ansible.builtin.systemd_service", {}).get("state") == "stopped"
        for t in enabled["rescue"]
    )
    names = [t["name"] for t in flat]
    assert names.index("Require immutable collector inputs") < names.index("Start inventory socket")
    user = by_name["Create the dedicated observer"]["ansible.builtin.user"]
    assert user["append"] is False and user["groups"] == [] and user["password_lock"] is True
    assert user["name"] == "kdive-authority-observer"
    install = by_name["Install the locked independent collector runtime"]
    assert (
        install["environment"]["UV_PROJECT_ENVIRONMENT"] == "/opt/kdive-authority-inventory/.venv"
    )
    assert install["environment"]["UV_PYTHON_DOWNLOADS"] == "never"
    assert "--no-editable" in install["ansible.builtin.command"]["argv"]
    assert "--reinstall-package" in install["ansible.builtin.command"]["argv"]


def test_units_and_identity():
    units = Path("deploy/systemd/system")
    service, sock = configparser.ConfigParser(interpolation=None), configparser.ConfigParser()
    service.read_string((units / "kdive-authority-inventory@.service").read_text())
    sock.read_string((units / "kdive-authority-inventory.socket").read_text())
    assert service["Service"]["User"] == "kdive-provider-authority"
    assert service["Service"]["StandardInput"] == "socket"
    assert service["Service"]["RuntimeMaxSec"] == "120s"
    assert service["Service"]["TimeoutStopSec"] == "1s"
    assert service["Unit"]["PartOf"] == "kdive-authority-inventory.socket"
    # A connection must never start a stopped or failed authority.
    assert service["Unit"]["Requisite"] == "kdive-external-boot-authority.service"
    assert "Requires" not in service["Unit"]
    assert sock["Socket"]["SocketUser"] == "root"
    assert sock["Socket"]["SocketMode"] == "0660"
    assert sock["Socket"]["SocketGroup"] == "kdive-authority-observer"
    assert sock["Socket"]["MaxConnections"] == "4" and sock["Socket"]["Backlog"] == "16"
    assert sock["Socket"]["Accept"] == "yes"
    assert "After" not in sock["Unit"]
    assert not {"environmentfile", "loadcredential", "passenvironment"} & set(service["Service"])
    assert service["Service"]["ExecStart"].startswith("/usr/bin/env -i PATH=/usr/bin:/bin ")


def test_collector_unit_carries_the_authority_hardening():
    """Every authority-unit protection the collector does not need relaxed is applied."""
    units = Path("deploy/systemd/system")
    service = configparser.ConfigParser(interpolation=None, strict=False)
    service.read_string((units / "kdive-authority-inventory@.service").read_text())
    for key in (
        "ProtectControlGroups",
        "ProtectKernelModules",
        "ProtectKernelTunables",
        "ProtectKernelLogs",
        "RestrictSUIDSGID",
        "RestrictRealtime",
        "LockPersonality",
        "MemoryDenyWriteExecute",
        "NoNewPrivileges",
        "PrivateDevices",
        "ProtectHome",
        "PrivateTmp",
    ):
        assert service["Service"][key] == "yes", key
    assert service["Service"]["ProtectSystem"] == "strict"
    assert service["Service"]["SystemCallArchitectures"] == "native"
    assert service["Service"]["CapabilityBoundingSet"] == ""


def test_teardown_withdraws_the_inventory_boundary_first():
    play = yaml.safe_load(Path("deploy/ansible/playbooks/authority_host_teardown.yml").read_text())
    names = [task["name"] for task in play[0]["tasks"]]
    withdraw = names.index(
        "Withdraw the proof-only inventory boundary before retiring the authority"
    )
    assert withdraw < names.index("Stop and disable the external-boot authority service")
    task = play[0]["tasks"][withdraw]
    assert (Path("deploy/ansible/playbooks") / task["ansible.builtin.include_tasks"]).resolve() == (
        SHARED.resolve()
    )
    assert task["vars"] == {
        "inventory_observer_enabled": False,
        "inventory_observer_authority_enabled": False,
    }
    inspected = next(
        t for t in play[0]["tasks"] if t["name"] == "Inspect removed authority endpoint artifacts"
    )
    assert {
        "/etc/systemd/system/kdive-authority-inventory.socket",
        "/etc/systemd/system/kdive-authority-inventory@.service",
        "/etc/tmpfiles.d/kdive-authority-inventory.conf",
        "/usr/libexec/kdive-authority-inventory-client.py",
        "/opt/kdive-authority-inventory",
        "/run/kdive-authority-inventory",
    } <= set(inspected["loop"])


def verifier():
    code = tasks()[0]["ansible.builtin.set_fact"]["provider_authority_host_inventory_verifier"]
    namespace = {"__name__": "offline_verifier"}
    exec(compile(code, str(SHARED), "exec"), namespace)
    return namespace


@pytest.mark.parametrize("fault", [None, "writable", "owner", "acl", "link", "ancestor"])
def test_runtime_verifier_rejects_mutability(monkeypatch, tmp_path, fault):
    ns = verifier()
    root = tmp_path / "runtime"
    root.mkdir()
    target = root / "module.py"
    target.write_text("pass\n")
    real_lstat = ns["os"].lstat

    def metadata(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        # Model root-controlled fixtures without changing actual accounts/ownership.
        uid = 102 if fault == "owner" and Path(path) == target else 0
        mode = st.st_mode & ~0o022
        if (
            fault == "writable"
            and Path(path) == target
            or fault == "ancestor"
            and Path(path) == root
        ):
            mode |= 0o020
        return SimpleNamespace(st_uid=uid, st_mode=mode)

    monkeypatch.setattr(ns["os"], "lstat", metadata)
    monkeypatch.setattr(
        ns["os"],
        "listxattr",
        lambda p, **kw: ["system.posix_acl_access"] if fault == "acl" and Path(p) == target else [],
    )
    if fault == "link":
        target.unlink()
        target.symlink_to(tmp_path / "missing")
    if fault:
        with pytest.raises((ValueError, OSError)):
            ns["trusted"](target)
    else:
        ns["trusted"](target)


@pytest.mark.parametrize("version", [(3, 13), (3, 14)])
def test_fixed_system_python_prerequisite(monkeypatch, version):
    prerequisite = next(
        t
        for t in flatten(tasks())
        if t["name"] == "Validate the fixed system interpreter prerequisite and existing identity"
    )
    enabled = next(t for t in tasks() if t["name"] == "Converge the enabled inventory boundary")
    assert prerequisite in enabled["block"]
    assert enabled["when"] == "inventory_observer_enabled | bool"
    argv = prerequisite["ansible.builtin.command"]["argv"]
    env = Environment(undefined=StrictUndefined)
    assert (
        env.from_string(argv[0]).render(
            provider_authority_host_python="/verified/python3.14",
            ansible_python_interpreter="/immutable/control/bin/python3",
        )
        == "/usr/bin/python3"
    )
    ns = verifier()
    monkeypatch.setattr(ns["sys"], "version_info", version)
    ns["trusted"] = Mock()
    if version == (3, 13):
        with pytest.raises(ValueError, match="system Python 3.14"):
            ns["system_python"]()
        ns["trusted"].assert_not_called()
    else:
        ns["system_python"]()
        assert ns["trusted"].called


def test_pth_escape_is_checked_before_runtime_execution(monkeypatch, tmp_path):
    ns = verifier()
    ns["ROOT"] = tmp_path
    pth = tmp_path / "escape.pth"
    pth.write_text("/untrusted/imports\n")
    guard = Mock(side_effect=ValueError("untrusted path"))
    ns["trusted"] = guard
    with pytest.raises(ValueError, match="untrusted path"):
        ns["pth_inputs"]()
    guard.assert_called_once_with(Path("/untrusted/imports"), tree=True)
    pth.write_text("import untrusted_code\n")
    guard.reset_mock()
    with pytest.raises(ValueError, match="executable pth"):
        ns["pth_inputs"]()
    guard.assert_not_called()


def test_runtime_verifier_binds_bytes_and_revision(monkeypatch, tmp_path):
    ns = verifier()
    source, install = tmp_path / "source", tmp_path / "installed"
    for root in (source / "src/kdive", install / "kdive", source / "deploy/authority-inventory"):
        root.mkdir(parents=True)
    (source / "src/kdive/__init__.py").write_text("")
    (install / "kdive/__init__.py").write_text("")
    ns["ROOT"] = install
    ns["CLIENT"] = install / "client.py"
    for name in ("client.py", "collector.py"):
        (source / "deploy/authority-inventory" / name).write_text("pass\n")
        (install / name).write_text("pass\n")
    ns["trusted"] = Mock()
    monkeypatch.setattr(ns["sysconfig"], "get_path", lambda _: str(install))
    monkeypatch.setattr(
        ns["importlib"].util, "find_spec", lambda _: SimpleNamespace(origin="/immutable")
    )
    monkeypatch.setattr(ns["subprocess"], "check_output", lambda _: b"src/kdive/__init__.py\0")
    ns["runtime"](source, "a" * 40)
    with pytest.raises(ValueError, match="invalid collector revision"):
        ns["runtime"](source, "wrong")
    (install / "client.py").write_text("changed\n")
    with pytest.raises(ValueError, match="script bytes differ"):
        ns["runtime"](source, "a" * 40)
    (install / "client.py").write_text("pass\n")
    (install / "kdive/__init__.py").write_text("changed\n")
    with pytest.raises(ValueError, match="package bytes differ"):
        ns["runtime"](source, "a" * 40)


@pytest.mark.parametrize("fault", [None, "root", "authority", "groups", "alias", "shell"])
def test_observer_identity_rejects_privilege(monkeypatch, fault):
    ns = verifier()
    account = SimpleNamespace(
        pw_name="kdive-authority-observer",
        pw_uid=101,
        pw_gid=101,
        pw_dir="/nonexistent",
        pw_shell="/usr/sbin/nologin",
    )
    authority = SimpleNamespace(pw_name="kdive-provider-authority", pw_uid=102, pw_gid=102)
    if fault == "root":
        account.pw_uid = 0
    elif fault == "authority":
        account.pw_uid = 102
    elif fault == "shell":
        account.pw_shell = "/bin/bash"
    monkeypatch.setattr(
        ns["pwd"], "getpwnam", lambda name: account if name == account.pw_name else authority
    )
    monkeypatch.setattr(
        ns["pwd"],
        "getpwall",
        lambda: (
            [account, authority]
            + (
                [SimpleNamespace(pw_name="other", pw_uid=101, pw_gid=999)]
                if fault == "alias"
                else []
            )
        ),
    )
    monkeypatch.setattr(
        ns["grp"],
        "getgrnam",
        lambda _: SimpleNamespace(gr_gid=101, gr_mem=[], gr_name="kdive-authority-observer"),
    )
    monkeypatch.setattr(ns["grp"], "getgrall", lambda: [])
    monkeypatch.setattr(
        ns["os"], "getgrouplist", lambda *a: [101, 102] if fault == "groups" else [101]
    )
    if fault:
        with pytest.raises(ValueError, match="identity collides"):
            ns["identity"](True)
    else:
        ns["identity"](True)


def test_installed_revision_marker(tmp_path):
    ns = verifier()
    ns["ROOT"] = tmp_path
    marker = tmp_path / "revision"
    marker.write_text("a" * 40 + "\n")
    marker.chmod(0o444)
    ns["revision_marker"]("a" * 40)
    with pytest.raises(ValueError, match="revision mismatch"):
        ns["revision_marker"]("b" * 40)
    marker.chmod(0o644)
    with pytest.raises(ValueError, match="revision mismatch"):
        ns["revision_marker"]("a" * 40)


def test_existing_privileged_group_refused_before_account_creation(monkeypatch):
    ns = verifier()
    monkeypatch.setattr(ns["grp"], "getgrnam", lambda _: SimpleNamespace(gr_gid=0, gr_mem=[]))
    account_lookup = Mock(side_effect=KeyError("absent observer"))
    monkeypatch.setattr(ns["pwd"], "getpwnam", account_lookup)
    with pytest.raises(ValueError, match="identity collides"):
        ns["identity"](False)
    account_lookup.assert_not_called()


@pytest.mark.parametrize("control_python", [None, "/immutable/control/bin/python3"])
def test_disabled_withdrawal_uses_control_interpreter(control_python):
    withdrawal = next(
        t
        for t in tasks()
        if t["name"] == "Verify ownership before changing or withdrawing collector files"
    )
    assert "when" not in withdrawal
    values = {"inventory_observer_enabled": False}
    if control_python is not None:
        values["ansible_python_interpreter"] = control_python
    argv = withdrawal["ansible.builtin.command"]["argv"]
    rendered = Environment(undefined=StrictUndefined).from_string(argv[0]).render(values)
    assert rendered == (control_python or "/usr/bin/python3")
    assert argv[1:4] == ["-I", "-S", "-c"] and argv[-1] == "withdraw"
    assert argv[4] == "{{ provider_authority_host_inventory_verifier }}"


@pytest.mark.parametrize(
    ("rc", "stdout", "stderr", "fails"),
    [
        (1, "", "User kdive-authority-observer is not allowed to run sudo on h.\n", False),
        (0, "User kdive-authority-observer is not allowed to run sudo on h.\n", "", False),
        (
            0,
            "User kdive-authority-observer may run the following commands on h:\n"
            "    (ALL) NOPASSWD: ALL\n",
            "",
            True,
        ),
        (2, "User kdive-authority-observer is not allowed to run sudo on h.\n", "", True),
        (
            0,
            "User kdive-authority-observer may run the following commands on h:\n",
            "User kdive-authority-observer is not allowed to run sudo on h.\n",
            True,
        ),
    ],
    ids=["classic-denial", "sudo-rs-denial", "granted", "unexpected-rc", "denial-beside-grant"],
)
def test_observer_sudo_refusal_accepts_only_a_denial(rc, stdout, stderr, fails):
    refusal = next(t for t in flatten(tasks()) if t["name"] == "Refuse observer sudo privileges")
    assert refusal["ansible.builtin.command"]["argv"] == [
        "sudo",
        "-n",
        "-l",
        "-U",
        "kdive-authority-observer",
    ]
    assert refusal["environment"] == {"LC_ALL": "C"} and refusal["no_log"] is True
    result = {"rc": rc, "stdout": stdout, "stderr": stderr}
    verdict = (
        Environment(undefined=StrictUndefined)
        .from_string("{{ " + refusal["failed_when"] + " }}")
        .render(provider_authority_host_inventory_sudo=result)
    )
    assert verdict == str(fails)


def uv_runtime(monkeypatch, tmp_path):
    """A collector root whose venv holds the world-writable lock `uv sync` leaves behind."""
    ns = verifier()
    root = tmp_path / "runtime"
    (root / ".venv").mkdir(parents=True)
    lock = root / ".venv/.lock"
    lock.touch()
    lock.chmod(0o666)
    ns["ROOT"] = root
    real_lstat = ns["os"].lstat

    def metadata(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        # Model root ownership; keep real modes inside the runtime, trusted ones above it.
        mode = st.st_mode if root in (Path(path), *Path(path).parents) else st.st_mode & ~0o022
        return SimpleNamespace(st_uid=0, st_mode=mode)

    monkeypatch.setattr(ns["os"], "lstat", metadata)
    monkeypatch.setattr(ns["os"], "listxattr", lambda p, **kw: [])
    return ns, root, lock


def test_uv_lock_is_removed_so_the_strict_guard_passes(monkeypatch, tmp_path):
    ns, root, lock = uv_runtime(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="not root-controlled"):
        ns["trusted"](root, tree=True)
    ns["unlock"]()
    assert not lock.exists()
    ns["trusted"](root, tree=True)
    ns["unlock"]()


@pytest.mark.parametrize("substitute", ["venv", "lock"])
def test_uv_lock_removal_refuses_a_substituted_path(monkeypatch, tmp_path, substitute):
    ns, root, lock = uv_runtime(monkeypatch, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".lock").touch()
    if substitute == "venv":
        (root / ".venv").rename(tmp_path / "moved")
        (root / ".venv").symlink_to(elsewhere)
    else:
        lock.unlink()
        lock.symlink_to(elsewhere / ".lock")
    with pytest.raises(ValueError, match="substituted inventory installation"):
        ns["unlock"]()
    assert (elsewhere / ".lock").exists()


def test_uv_lock_is_removed_before_every_verifier_can_see_it():
    top = tasks()
    names = [t["name"] for t in top]
    unlock = "Remove uv's install lock from the collector runtime"
    assert names.index(unlock) < names.index(
        "Verify ownership before changing or withdrawing collector files"
    )
    block = [
        t["name"] for t in top[names.index("Converge the enabled inventory boundary")]["block"]
    ]
    install = block.index("Install the locked independent collector runtime")
    assert block.index("Remove uv's install lock after the runtime sync") == install + 1
    for task in (t for t in flatten(top) if "install lock" in t["name"]):
        assert task["ansible.builtin.command"]["argv"][-1] == "unlock"


def test_uv_lock_removal_requires_a_root_controlled_venv(monkeypatch, tmp_path):
    ns, root, lock = uv_runtime(monkeypatch, tmp_path)
    (root / ".venv").chmod(0o777)
    with pytest.raises(ValueError, match="not root-controlled"):
        ns["unlock"]()
    assert lock.exists()


@pytest.mark.skipif(
    Path("/opt/kdive-authority-inventory").exists(), reason="host has a collector runtime"
)
def test_unlock_phase_runs_no_other_verifier_phase(monkeypatch):
    code = tasks()[0]["ansible.builtin.set_fact"]["provider_authority_host_inventory_verifier"]
    monkeypatch.setattr("sys.argv", ["-c", "unlock"])
    with pytest.raises(SystemExit) as exited:
        exec(compile(code, str(SHARED), "exec"), {"__name__": "__main__"})
    assert exited.value.code is None
