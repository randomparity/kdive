"""Drive the real authority role tasks; replace only firewall-system module effects."""

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def play(name, variables, env, *, passes=True):
    result = subprocess.run(
        [
            "ansible-playbook",
            str(HERE / f"{name}.yml"),
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


def main():
    with tempfile.TemporaryDirectory(
        prefix="kdive-authority-firewall-", dir=Path.home()
    ) as scratch:
        root = Path(scratch)
        env = os.environ.copy()
        env["ANSIBLE_CONFIG"] = str(HERE.parent / "ansible.cfg")
        env["ANSIBLE_ROLES_PATH"] = str(HERE.parent / "roles")
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
        for family in ("Debian", "RedHat"):
            (root / "rules.json").write_text("[]")
            variables = {
                "ansible_os_family": family,
                "worker_cidr": "192.0.2.0/24",
                "gdbstub_range": "47000:47099",
                "gdbstub_acl_authority_port": None,
                "gdbstub_acl_authority_state_path": str(root / "managed/authority.json"),
            }
            play("authority_firewall", variables, env)
            assert not (root / "managed/authority.json").exists()
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
            original = marker.read_text()
            for malformed in ("{}", '{"port":22,"source":"::/0"}', "[]", "not-json"):
                marker.write_text(malformed)
                before = (root / "calls.jsonl").read_text()
                play("authority_firewall", variables, env, passes=False)
                assert (root / "calls.jsonl").read_text() == before
            marker.write_text(original)
            parent = marker.parent
            retained = root / "retained"
            parent.rename(retained)
            parent.symlink_to(retained, target_is_directory=True)
            before = (root / "calls.jsonl").read_text()
            play("authority_firewall", variables, env, passes=False)
            assert (root / "calls.jsonl").read_text() == before
            parent.unlink()
            retained.rename(parent)
            variables["gdbstub_acl_authority_port"] = None
            play("authority_firewall", variables, env)
            check_rules(root, family, None, "198.51.100.0/24")
            assert not marker.exists()
            output = play("authority_firewall", variables, env)
            assert "changed=0 " in output, output
            print(
                f"authority_firewall {family}: enable, drift, idempotence, "
                "corruption, disable passed"
            )
        variables = {"ansible_os_family": "Debian"}
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
