"""Drive the real authority role tasks; replace only firewall-system module effects."""

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def play(name, variables, env, *, passes=True):
    result = subprocess.run(
        [
            "ansible-playbook",
            str(Path(name) if Path(name).is_absolute() else HERE / f"{name}.yml"),
            "-i",
            "localhost,",
            "--tags",
            "authority_firewall,provider_authority_preflight",
            "-e",
            json.dumps(variables),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) == passes, result.stdout + result.stderr
    return result.stdout


def check_rules(root, family, port, source):
    rules = json.loads((root / "rules.json").read_text())
    protected = json.loads((root / "protected.json").read_text())
    assert all(rule in rules for rule in protected), "unrelated protected rules changed"
    rules = [rule for rule in rules if rule not in protected]
    if port is None:
        assert rules == [], rules
    elif family == "RedHat":
        assert rules == [
            {
                "module": "ansible.posix.firewalld",
                "args": {
                    "rich_rule": f'rule priority="100" family="ipv4" port port="{port}" '
                    'protocol="tcp" drop'
                },
            },
            {
                "module": "ansible.posix.firewalld",
                "args": {
                    "rich_rule": f'rule family="ipv4" source address="{source}" '
                    f'port port="{port}" protocol="tcp" accept'
                },
            },
        ], rules
    else:
        assert rules == [
            {
                "module": "community.general.ufw",
                "args": {"rule": "deny", "direction": "in", "proto": "tcp", "to_port": str(port)},
            },
            {
                "module": "community.general.ufw",
                "args": {
                    "rule": "allow",
                    "direction": "in",
                    "proto": "tcp",
                    "to_port": str(port),
                    "from_ip": source,
                },
            },
        ], rules


def check_interrupted_transition(root, family, variables, env):
    marker = root / "managed/authority.json"
    for first_enable in (True, False):
        for disable_next in (True, False):
            selected = variables | {"gdbstub_acl_authority_port": 18443}
            if not first_enable:
                play("authority_firewall", selected, env)
            selected["worker_cidr"] = "198.51.100.0/24"
            play(
                "authority_firewall",
                selected,
                env | {"FAIL_AFTER_AUTHORITY_GRANT": "1"},
                passes=False,
            )
            check_rules(root, family, 18443, "198.51.100.0/24")
            assert marker.exists(), "a granted tuple lost its durable ownership on first enable"
            assert json.loads(marker.read_text())["pending"] == {
                "port": 18443,
                "source": "198.51.100.0/24",
            }, "an interrupted grant must retain its pending tuple"
            selected.update(
                worker_cidr="203.0.113.0/24",
                gdbstub_acl_authority_port=None if disable_next else 18443,
            )
            play("authority_firewall", selected, env)
            check_rules(root, family, selected["gdbstub_acl_authority_port"], "203.0.113.0/24")
            selected["gdbstub_acl_authority_port"] = None
            play("authority_firewall", selected, env)
            check_rules(root, family, None, "203.0.113.0/24")
            assert not marker.exists()
    print(f"authority_firewall {family}: interrupted first enable and drift recover/disable passed")


def check_retained_pair(root, family, variables, env):
    selected = variables | {"gdbstub_acl_authority_port": 18443}
    play("authority_firewall", selected, env)
    rules = json.loads((root / "rules.json").read_text())
    pending_allow = json.loads(json.dumps(rules[-1]).replace("192.0.2.0/24", "198.51.100.0/24"))
    (root / "rules.json").write_text(json.dumps(rules + [pending_allow]))
    (root / "managed/authority.json").write_text(
        json.dumps(
            {
                "current": {"port": 18443, "source": "192.0.2.0/24"},
                "pending": {"port": 18443, "source": "198.51.100.0/24"},
            }
        )
    )
    play("authority_firewall", variables, env)
    check_rules(root, family, None, "192.0.2.0/24")
    print(f"authority_firewall {family}: both retained current/pending tuples retract passed")


def check_marker_refusals(root, variables, env):
    marker = root / "managed/authority.json"
    original = marker.read_text()
    original_value = json.loads(original)
    bad_tuple = {"port": 16514, "source": "192.0.2.0/24"}
    for malformed in (
        "{}",
        '{"port":22,"source":"::/0"}',
        "[]",
        "not-json",
        " " * 513 + original,
        '{"current":null,"pending":null}',
        original[:-1] + ', "pending": null}',
        json.dumps(original_value | {"extra": None}),
        json.dumps(original_value | {"pending": bad_tuple}),
        json.dumps(original_value | {"pending": bad_tuple | {"port": 22}}),
        json.dumps(original_value | {"pending": bad_tuple | {"port": 47001}}),
        json.dumps(original_value | {"pending": bad_tuple | {"port": 18443, "source": "::/0"}}),
    ):
        marker.write_text(malformed)
        before_backend = (root / "backend-calls.jsonl").read_text()
        before = (root / "calls.jsonl").read_text()
        play("authority_firewall", variables, env, passes=False)
        assert (root / "calls.jsonl").read_text() == before
        assert (root / "backend-calls.jsonl").read_text() == before_backend
    marker.write_text(original)
    for path, unsafe, safe in ((marker, 0o644, 0o600), (marker.parent, 0o755, 0o700)):
        path.chmod(unsafe)
        before_backend = (root / "backend-calls.jsonl").read_text()
        before = (root / "calls.jsonl").read_text()
        play("authority_firewall", variables, env, passes=False)
        assert (root / "calls.jsonl").read_text() == before
        assert (root / "backend-calls.jsonl").read_text() == before_backend
        path.chmod(safe)
    for path in (marker, marker.parent):
        retained = path.with_name(path.name + "-retained")
        path.rename(retained)
        path.symlink_to(retained, target_is_directory=retained.is_dir())
        before_backend = (root / "backend-calls.jsonl").read_text()
        before = (root / "calls.jsonl").read_text()
        play("authority_firewall", variables, env, passes=False)
        assert (root / "calls.jsonl").read_text() == before
        assert (root / "backend-calls.jsonl").read_text() == before_backend
        path.unlink()
        retained.rename(path)


def replace_backend_tasks(tasks):
    for task in tasks:
        for operation in ("package", "systemd_service", "service_facts", "stat"):
            key = "ansible.builtin." + operation
            if key in task:
                task["kdive.test.backend"] = {"operation": operation, **(task.pop(key) or {})}
        for key in ("block", "rescue", "always"):
            if key in task:
                replace_backend_tasks(task[key])


def reset_backend(root, *, running=False):
    (root / "backend.json").write_text(
        json.dumps(
            {
                "packages": ["firewalld", "python3-firewall"] if running else [],
                "running": running,
                "enabled": running,
                "management": [],
            }
        )
    )
    (root / "backend-calls.jsonl").write_text("")


def check_backend_contract(root, family, variables, env):
    selected = variables | {"ansible_port": 2222, "gdbstub_acl_authority_port": 18443}
    # This first real-role call must install the missing backend before its first rule.
    play("authority_firewall", selected, env)
    state = json.loads((root / "backend.json").read_text())
    assert set(state["packages"]) == (
        {"firewalld", "python3-firewall"} if family == "RedHat" else {"ufw"}
    )
    if family == "RedHat":
        assert state["running"] and state["enabled"] and state["management"] == ["2222/tcp"]
        assert "changed=0 " in play("authority_firewall", selected, env)
        reset_backend(root, running=True)
        play("authority_firewall", selected, env)
        assert json.loads((root / "backend.json").read_text())["management"] == []
    play("authority_firewall", variables, env)
    failures = ("package", "management", "systemd_service") if family == "RedHat" else ("package",)
    for failure in failures:
        reset_backend(root)
        before = (root / "rules.json").read_text()
        play("authority_firewall", selected, env | {"FAIL_BACKEND": failure}, passes=False)
        assert (root / "rules.json").read_text() == before
        assert not json.loads((root / "backend.json").read_text())["running"]
    if family == "RedHat":
        for port in (0, 65536, 16514, 47000, 47099):
            reset_backend(root)
            play("authority_firewall", selected | {"ansible_port": port}, env, passes=False)
            state = json.loads((root / "backend.json").read_text())
            assert not state["running"] and state["management"] == []
    check_rescue(root, variables, env)
    reset_backend(root)
    print(f"backend {family}: first install, management, running policy, failures passed")


def check_rescue(root, variables, env):
    source = yaml.safe_load(
        (HERE.parent / "roles/provider_authority_host/tasks/main.yml").read_text()
    )
    convergence = next(task for task in source if "block" in task)
    replace_backend_tasks([convergence])
    for task in convergence["block"]:
        if "ansible.builtin.include_role" in task:
            # Restrict the external role boundary to its real prerequisite/authority tasks.
            task["ansible.builtin.include_role"]["tasks_from"] = "authority.yml"
        if "ansible.builtin.include_tasks" in task:
            task.pop("ansible.builtin.include_tasks")
            task["ansible.builtin.fail"] = {"msg": "controlled post-firewall failure"}
    convergence["tags"] = ["authority_firewall"]
    path = root / "rescue.yml"
    path.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "tasks": [convergence],
                }
            ],
            sort_keys=False,
        )
    )
    selected = variables | {"provider_authority_host_enabled": False}
    failures = ["package"]
    if variables["ansible_facts"]["os_family"] == "RedHat":
        failures.extend(["management", "systemd_service"])
    cases = [(failure, "Protected-port firewall convergence") for failure in failures]
    for failure, phase in [*cases, ("", "Disabled provider authority cleanup")]:
        reset_backend(root)
        output = play(str(path), selected, env | {"FAIL_BACKEND": failure}, passes=False)
        assert phase + " failed;" in output, output
        assert json.loads((root / "backend.json").read_text())["authority_stopped"]


def main():
    with tempfile.TemporaryDirectory(
        prefix="kdive-authority-firewall-", dir=Path.home()
    ) as scratch:
        root = Path(scratch)
        env = os.environ.copy()
        env["ANSIBLE_CONFIG"] = str(HERE.parent / "ansible.cfg")
        roles = root / "roles"
        shutil.copytree(HERE.parent / "roles/gdbstub_acl", roles / "gdbstub_acl")
        for path in (roles / "gdbstub_acl/tasks").glob("*.yml"):
            tasks = yaml.safe_load(path.read_text())
            replace_backend_tasks(tasks)
            path.write_text(yaml.safe_dump(tasks, sort_keys=False))
        env["ANSIBLE_ROLES_PATH"] = str(roles) + ":" + str(HERE.parent / "roles")
        env["ANSIBLE_INJECT_FACT_VARS"] = "False"
        env["FAKE_AUTHORITY_FIREWALL_ROOT"] = str(root)
        env["ANSIBLE_COLLECTIONS_PATH"] = (
            str(root / "collections") + ":" + str(Path.home() / ".ansible/collections")
        )
        for namespace, collection, module in (
            ("ansible", "posix", "firewalld"),
            ("community", "general", "ufw"),
        ):
            dest = (
                root / "collections/ansible_collections" / namespace / collection / "plugins/action"
            )
            dest.mkdir(parents=True)
            shutil.copyfile(HERE / "firewall_action.py", dest / f"{module}.py")
        dest = root / "collections/ansible_collections/kdive/test/plugins/action"
        dest.mkdir(parents=True)
        shutil.copyfile(HERE / "backend_action.py", dest / "backend.py")
        for family in ("Debian", "RedHat"):
            reset_backend(root)
            if family == "RedHat":
                protected = [
                    {
                        "module": "ansible.posix.firewalld",
                        "args": {
                            "rich_rule": f'rule port port="{port}" protocol="tcp" accept',
                        },
                    }
                    for port in ("22", "16514", "47000-47099")
                ]
            else:
                protected = [
                    {
                        "module": "community.general.ufw",
                        "args": {
                            "rule": "allow",
                            "direction": "in",
                            "proto": "tcp",
                            "to_port": port,
                        },
                    }
                    for port in ("22", "16514", "47000:47099")
                ]
            (root / "protected.json").write_text(json.dumps(protected))
            (root / "rules.json").write_text(json.dumps(protected))
            variables = {
                "ansible_facts": {"os_family": family},
                "ansible_python_interpreter": sys.executable,
                "worker_cidr": "192.0.2.0/24",
                "gdbstub_range": "47000:47099",
                "gdbstub_acl_authority_port": None,
                "gdbstub_acl_authority_state_path": str(root / "managed/authority.json"),
            }
            check_backend_contract(root, family, variables, env)
            play("authority_firewall", variables, env)
            assert not (root / "managed/authority.json").exists()
            check_interrupted_transition(root, family, variables, env)
            check_retained_pair(root, family, variables, env)
            variables["gdbstub_acl_authority_port"] = 18443
            play("authority_firewall", variables, env)
            check_rules(root, family, 18443, "192.0.2.0/24")
            assert (root / "managed/authority.json").stat().st_mode & 0o777 == 0o600
            output = play("authority_firewall", variables, env)
            assert "changed=0 " in output, output
            variables.update(worker_cidr="198.51.100.0/24", gdbstub_acl_authority_port=18444)
            play("authority_firewall", variables, env)
            check_rules(root, family, 18444, "198.51.100.0/24")
            marker = root / "managed/authority.json"
            check_marker_refusals(root, variables, env)
            variables["gdbstub_acl_authority_port"] = None
            play("authority_firewall", variables, env)
            check_rules(root, family, None, "198.51.100.0/24")
            assert not marker.exists()
            output = play("authority_firewall", variables, env)
            assert "changed=0 " in output, output
            calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
            assert all(
                {"module": call["module"], "args": call["args"]} not in protected for call in calls
            ), "an unrelated protected rule was touched"
            print(
                f"authority_firewall {family}: enable, drift, idempotence, "
                "corruption, disable passed"
            )
        variables = {}
        play("provider_authority_preflight", variables, env)
        for invalid in (
            {"provider_authority_host_network_address": "127.0.0.1"},
            {"provider_authority_host_network_port": 18443},
            {
                "provider_authority_host_network_address": "::1",
                "provider_authority_host_network_port": 18443,
            },
            {"provider_authority_host_enabled": True},
        ):
            play("provider_authority_preflight", variables | invalid, env, passes=False)
        print("provider_authority_preflight: default disabled and partial input refusal passed")
        source = root / "credential"
        source.write_text("test-material")
        source.chmod(0o600)
        complete = variables | {
            "provider_authority_host_enabled": True,
            "provider_authority_host_instance": "authority-test",
            "provider_authority_host_source_root": str(root),
            "provider_authority_host_python": sys.executable,
            "provider_authority_host_uv_bin": shutil.which("uv"),
            "worker_cidr": "192.0.2.0/24",
            "provider_authority_host_network_address": "127.0.0.1",
            "provider_authority_host_network_port": 18443,
        }
        for name in (
            "database_dsn",
            "server_key",
            "server_certificate",
            "server_ca",
            "worker_client_ca",
            "health_client_certificate",
            "health_client_key",
        ):
            complete[f"provider_authority_host_{name}_source"] = str(source)
        play("provider_authority_preflight", complete, env)
        account = pwd.getpwuid(os.getuid()).pw_name
        for extra in (
            [account],
            ["nonexistent-kdive-test-account"],
            ["Invalid"],
            [""],
            ["account"] * 32,
            ["a" * 33],
            ["root"],
        ):
            output = play(
                "provider_authority_preflight",
                complete | {"provider_authority_host_additional_denied_identities": extra},
                env,
                passes=False,
            )
            # Ansible's source-location path can contain the controller's account name.
            # Assert against serialized values, not unrelated filesystem path components.
            assert all(json.dumps(name) not in output for name in extra if name), output
        for invalid in (
            {"provider_authority_host_network_address": "::1"},
            {"provider_authority_host_network_address": "224.0.0.1"},
            {"provider_authority_host_network_address": "localhost"},
            {"provider_authority_host_network_port": 0},
            {"provider_authority_host_network_port": 65536},
            {"provider_authority_host_network_port": 16514},
            {"provider_authority_host_fault_proof_enabled": "true"},
            {"worker_cidr": "::/0"},
            {"worker_cidr": "0.0.0.0/0"},
        ):
            play("provider_authority_preflight", complete | invalid, env, passes=False)
        source.chmod(0o644)
        play("provider_authority_preflight", complete, env, passes=False)
        print(
            "provider_authority_preflight: complete, identities, unsafe inputs, source modes passed"
        )


if __name__ == "__main__":
    main()
