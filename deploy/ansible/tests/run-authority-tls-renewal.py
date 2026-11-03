#!/usr/bin/env python3
"""Exercise the real renewal playbook's controller checks and defaults loading (#3273)."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
ANSIBLE = ROOT / "deploy/ansible"
PLAYBOOK = ANSIBLE / "playbooks/renew-local-authority-tls.yml"
PKI = ROOT / "scripts/operations/authority-pki.sh"
INSTANCE = "renewal-harness-instance"
CROSS_CHECK = (
    "Derive the authority server name on the controller",
    "Require each source certificate to match its key",
)
DEFAULTS_PROBE = (
    "group={{ live_vm_host_authority_client_group }}"
    " accounts={{ live_vm_host_worker_accounts | join(',') }}"
)
REVISION_CHECK = (
    "Read the controller checkout state",
    "Require a clean controller checkout at exactly the installed revision",
)


def playbook_tasks(first: str, last: str) -> list[dict[str, object]]:
    tasks = yaml.safe_load(PLAYBOOK.read_text())[0]["tasks"]
    names = [task["name"] for task in tasks]
    return tasks[names.index(first) : names.index(last) + 1]


def ansible(play: Path, inventory: str = "localhost,") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ansible-playbook", "-i", inventory, str(play)],
        env={**os.environ, "ANSIBLE_CONFIG": str(ANSIBLE / "ansible.cfg")},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def write_play(path: Path, tasks: list[dict[str, object]], variables: dict[str, object]) -> None:
    path.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "vars": {"ansible_python_interpreter": sys.executable, **variables},
                    "tasks": [*tasks, {"ansible.builtin.debug": {"msg": "harness-completed"}}],
                }
            ]
        )
    )


def issue(directory: Path, instance: str = INSTANCE) -> Path:
    directory.mkdir(mode=0o700)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def run(*args: str) -> None:
        subprocess.run(["bash", str(PKI), *args, "--dir", str(directory)], env=env, check=True,
                       capture_output=True)  # fmt: skip

    run("ca", "--subject", f"{directory.name}-ca", "--days", "30", "--key", "new")
    run("leaf", "--name", "server", "--usage", "server", "--days", "7", "--key", "new",
        "--authority-instance", instance)  # fmt: skip
    for client in ("health-client", "worker-client"):
        run("leaf", "--name", client, "--usage", "client", "--days", "7", "--key", "new")
    return directory


def sources(main: Path, **overrides: Path) -> dict[str, object]:
    paths = {
        "server_certificate": main / "server.pem",
        "server_key": main / "server.key",
        "server_ca": main / "ca.pem",
        "worker_server_ca": main / "ca.pem",
        "worker_client_ca": main / "ca.pem",
        "health_certificate": main / "health-client.pem",
        "health_key": main / "health-client.key",
        "worker_certificate": main / "worker-client.pem",
        "worker_key": main / "worker-client.key",
    } | overrides
    return {
        "renew_authority_pki": str(PKI),
        "live_vm_host_authority_instance": INSTANCE,
        "live_vm_host_authority_server_certificate_source": str(paths["server_certificate"]),
        "live_vm_host_authority_server_key_source": str(paths["server_key"]),
        "live_vm_host_authority_server_ca_source": str(paths["server_ca"]),
        "live_vm_host_worker_authority_server_ca_source": str(paths["worker_server_ca"]),
        "live_vm_host_authority_worker_client_ca_source": str(paths["worker_client_ca"]),
        "live_vm_host_authority_health_client_certificate_source": str(paths["health_certificate"]),
        "live_vm_host_authority_health_client_key_source": str(paths["health_key"]),
        "live_vm_host_worker_authority_client_certificate_source": str(paths["worker_certificate"]),
        "live_vm_host_worker_authority_client_key_source": str(paths["worker_key"]),
    }


def cross_check(work: Path, name: str, variables: dict[str, object], failure: str | None) -> None:
    play = work / f"{name}.yml"
    write_play(play, playbook_tasks(*CROSS_CHECK), variables)
    result = ansible(play)
    text = result.stdout + result.stderr
    assert (result.returncode != 0) == bool(failure), (name, text)
    assert ("harness-completed" in text) != bool(failure), (name, text)
    if failure:
        assert failure in text, (name, text)
    assert "PRIVATE KEY" not in text, name
    print(f"ok cross-check {name}")


def cross_check_cases(work: Path) -> None:
    main, other = issue(work / "main"), issue(work / "other")
    renamed = issue(work / "renamed", instance="another-instance")
    cross_check(work, "matching", sources(main), None)
    cross_check(
        work,
        "server-from-another-ca",
        sources(main, server_certificate=other / "server.pem", server_key=other / "server.key"),
        "server-certificate against the worker server-ca",
    )
    cross_check(
        work,
        "worker-client-from-another-ca",
        sources(main, worker_certificate=other / "worker-client.pem",
                worker_key=other / "worker-client.key"),
        "worker client-certificate against worker-client-ca",
    )  # fmt: skip
    cross_check(
        work,
        "server-name-mismatch",
        sources(main, server_certificate=renamed / "server.pem", server_ca=renamed / "ca.pem",
                worker_server_ca=renamed / "ca.pem", server_key=renamed / "server.key"),
        "hostname mismatch",
    )  # fmt: skip
    cross_check(
        work,
        "key-mismatch",
        sources(main, health_key=main / "worker-client.key"),
        "health-client-certificate does not match its private key source",
    )


def revision_case(
    work: Path, name: str, *, dirty: bool, version: str | None, installed: str | None
) -> None:
    repo = work / f"repo-{name}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    git = ["git", "-C", str(repo), "-c", "user.name=h", "-c", "user.email=h@example.invalid"]
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], env=env, check=True)
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"], env=env, check=True, capture_output=True, text=True
    ).stdout.strip()
    # The play lives in the checkout (playbook_dir) but is ignored, so only `dirty` dirties it.
    (repo / ".git" / "info" / "exclude").write_text("play.yml\n")
    if dirty:
        (repo / "untracked").write_text("x")
    runtime = work / f"runtime-{name}"
    runtime.mkdir()
    (runtime / "revision").write_text(f"{installed or head}\n")
    play = repo / "play.yml"
    write_play(
        play,
        playbook_tasks(*REVISION_CHECK),
        {
            "live_vm_repo_version": version or head,
            "live_vm_host_authority_runtime_install": str(runtime),
        },
    )
    result = ansible(play)
    text = result.stdout + result.stderr
    failure = dirty or version is not None or installed is not None
    assert (result.returncode != 0) == failure, (name, text)
    assert ("harness-completed" in text) != failure, (name, text)
    if failure:
        assert "Renewal runs only the installed revision's tasks" in text, (name, text)
    print(f"ok revision {name}")


def defaults_case(work: Path) -> None:
    first = yaml.safe_load(PLAYBOOK.read_text())[0]["tasks"][0]
    inventory = work / "inventory"
    (inventory / "group_vars").mkdir(parents=True)
    (inventory / "hosts.yml").write_text(
        yaml.safe_dump({"all": {"children": {"live_vm_runners": {"hosts": {"localhost": None}}}}})
    )
    (inventory / "group_vars" / "live_vm_runners.yml").write_text(
        "live_vm_host_authority_client_group: harness-override\n"
    )
    play = work / "defaults.yml"
    play.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "live_vm_runners",
                    "connection": "local",
                    "gather_facts": False,
                    "vars": {"ansible_python_interpreter": sys.executable},
                    "tasks": [
                        first,
                        {"ansible.builtin.debug": {"msg": DEFAULTS_PROBE}},
                    ],
                }
            ]
        )
    )
    result = subprocess.run(
        ["ansible-playbook", "-i", str(inventory / "hosts.yml"), str(play)],
        env={**os.environ, "ANSIBLE_CONFIG": str(ANSIBLE / "ansible.cfg"),
             "ANSIBLE_ROLES_PATH": str(ANSIBLE / "roles")},
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )  # fmt: skip
    text = result.stdout + result.stderr
    assert result.returncode == 0, text
    assert "group=harness-override accounts=kdive-worker-1,kdive-worker-2" in text, text
    assert "changed=0" in text and "skipped=" in text, text
    print("ok defaults: inventory overrides win and every local_worker_host task is skipped")


with tempfile.TemporaryDirectory(prefix="kdive-tls-renewal-test-") as directory:
    work = Path(directory)
    cross_check_cases(work)
    revision_case(work, "clean-exact", dirty=False, version=None, installed=None)
    revision_case(work, "dirty", dirty=True, version=None, installed=None)
    revision_case(work, "installed-differs", dirty=False, version=None, installed="0" * 40)
    revision_case(work, "vars-differ", dirty=False, version="1" * 40, installed=None)
    defaults_case(work)
print("ok authority-tls-renewal")
