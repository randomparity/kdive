"""Run production package tasks with real uv against a same-path source-only upgrade."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def run(argv, **kwargs):
    result = subprocess.run(argv, text=True, capture_output=True, check=False, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def installation_play(root):
    tasks = yaml.safe_load(
        (HERE.parent / "roles/provider_authority_host/tasks/install.yml").read_text()
    )
    selected_names = {
        "Read the staged authority source revision",
        "Require a clean staged authority checkout",
        "Inspect the installed authority revision",
        "Refuse a substituted installed authority revision",
        "Read the installed authority revision",
        "Determine authority source revision drift",
        "Install the authority from the staged checkout and frozen dependency lock",
        "Prove the installed project bytes match the clean source revision",
        "Record the installed authority revision after successful installation",
    }
    selected = [task for task in tasks if task["name"] in selected_names]
    assert {task["name"] for task in selected} == selected_names
    for task in selected:
        if "ansible.builtin.copy" in task:
            task["ansible.builtin.copy"].update(owner=str(os.getuid()), group=str(os.getgid()))
    document = [
        {
            "name": "Exercise installed authority revision convergence",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "tasks": selected,
            "handlers": [
                {
                    "name": "Restart provider authority after deployed changes",
                    "ansible.builtin.copy": {
                        "dest": str(root / "restart"),
                        "content": "{{ provider_authority_host_source_revision.stdout }}\n",
                        "mode": "0600",
                    },
                }
            ],
        }
    ]
    path = root / "install.yml"
    path.write_text(
        yaml.safe_dump(document).replace("/opt/kdive-provider-authority", str(root / "install"))
    )
    return path


def check_base_destination_guards(root):
    tasks = []
    for role, filename, prefix, remote in (
        ("provider_authority_host", "install.yml", "provider_authority_host_system", True),
        ("live_vm_host", "main.yml", "live_vm_host_authority_system", False),
    ):
        source = yaml.safe_load((HERE.parent / "roles" / role / "tasks" / filename).read_text())
        inspect = next(
            task
            for task in source
            if task["name"] == "Inspect authority System base destinations without following links"
        )
        expected = [
            "/var/lib/kdive/provider-authority/system-provisioning/local/rootfs/bases/"
            + "a" * 64
            + ".qcow2"
        ]
        if remote:
            expected.append("/var/lib/kdive/provider-authority/remote-libvirt-pool/base.qcow2")
        tasks.extend(
            [
                {
                    "name": "Render " + role + " base guard destinations",
                    "ansible.builtin.set_fact": {"guard_paths": inspect["loop"]},
                    "vars": {
                        prefix + "_local_bases": [{"digest": "a" * 64}],
                        prefix + "_remote_bases": [{"name": "base.qcow2"}],
                    },
                },
                {
                    "name": "Require actual selected base paths",
                    "ansible.builtin.assert": {"that": "guard_paths == expected_paths"},
                    "vars": {"expected_paths": expected},
                },
            ]
        )
    play = root / "base-guards.yml"
    play.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Verify base guard rendering",
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "tasks": tasks,
                }
            ]
        )
    )
    run(["ansible-playbook", "-i", "localhost,", str(play)])


def check_appliance_consumer(root):
    tasks = yaml.safe_load(
        (HERE.parent / "roles/provider_authority_host/tasks/install.yml").read_text()
    )
    task = next(
        t
        for t in tasks
        if t["name"] == "Verify selected installed appliances as the authority before service start"
    )
    assert task.pop("become") and task.pop("become_user") == "kdive-provider-authority"
    task["become"] = False
    argv = task["ansible.builtin.command"]["argv"]
    appliance = root / "appliance"
    root.chmod(0o755)
    for architecture in ["x86_64", "ppc64le"]:
        directory = appliance / architecture
        (directory / "image").mkdir(parents=True)
        for parent in (appliance, directory, directory / "image"):
            parent.chmod(0o755)
        files = {"image/vmlinuz": b"kernel", "image/initramfs.cpio": b"initramfs"}
        for name, content in files.items():
            (directory / name).write_bytes(content)
            (directory / name).chmod(0o644)
        manifest = {
            "architecture": architecture,
            "format": "kdive-remote-module-appliance-v1",
            "initramfs_files": [],
            "files": [
                {
                    "path": name,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for name, content in files.items()
            ],
        }
        (directory / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        )
        (directory / "manifest.json").chmod(0o644)
    # Import while the checkout is reachable, then drop root's DAC bypass before the
    # unmodified production command opens any fixture. No system account is created.
    wrapper = (
        "import os, pwd\nfrom pathlib import Path\n"
        "from kdive.providers.external_boot_authority import host\n"
        "from kdive.providers.remote_libvirt import remote_module_authority_host\n"
        f"host._REMOTE_MODULE_APPLIANCE_ROOT = Path({str(appliance)!r})\n"
        "if os.geteuid() == 0:\n"
        "    account = pwd.getpwnam('nobody')\n"
        "    os.setgroups([])\n    os.setgid(account.pw_gid)\n    os.setuid(account.pw_uid)\n"
    )
    argv[0] = sys.executable
    argv[2] = wrapper + argv[2]
    marker = root / "appliance-start"
    play = root / "appliance-check.yml"
    play.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "vars": {
                        "provider_authority_host_remote_module_enabled": True,
                        "provider_authority_host_remote_module_architectures": [
                            "x86_64",
                            "ppc64le",
                        ],
                    },
                    "tasks": [
                        task,
                        {
                            "name": "Record reaching service start",
                            "ansible.builtin.copy": {
                                "dest": str(marker),
                                "content": "started",
                                "mode": "0600",
                            },
                        },
                    ],
                }
            ]
        )
    )

    def check(passes, error=""):
        marker.unlink(missing_ok=True)
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(play)],
            text=True,
            capture_output=True,
            check=False,
            timeout=150,
        )
        assert (result.returncode == 0) == passes, result.stdout + result.stderr
        assert marker.exists() == passes, result.stdout + result.stderr
        assert error in result.stdout + result.stderr

    check(True)
    kernel = appliance / "x86_64/image/vmlinuz"
    kernel.write_bytes(b"broken")
    check(False, "installed appliance file differs from its manifest")
    kernel.write_bytes(b"kernel")
    try:
        appliance.chmod(0)
        check(False, "PermissionError")
    finally:
        appliance.chmod(0o755)
    check(True)


def main():
    with tempfile.TemporaryDirectory(prefix="kdive-authority-install-") as scratch:
        root = Path(scratch)
        check_base_destination_guards(root)
        check_appliance_consumer(root)
        source = root / "source"
        package = source / "src/kdive"
        package.mkdir(parents=True)
        (root / "install").mkdir()
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        (source / "pyproject.toml").write_text(
            "[build-system]\nrequires = "
            + json.dumps(metadata["build-system"]["requires"])
            + '\nbuild-backend = "uv_build"\n[project]\nname = "kdive"\n'
            'version = "0.0.0"\nrequires-python = ">=3.14"\n'
        )
        (package / "__init__.py").write_text('REVISION = "first"\n')
        env = os.environ | {"UV_PYTHON_DOWNLOADS": "never", "UV_LINK_MODE": "copy"}
        run(["uv", "lock", "--project", str(source), "--python", sys.executable], env=env)
        run(["git", "init", "--quiet", str(source)])

        def commit():
            run(["git", "-C", str(source), "add", "pyproject.toml", "uv.lock", "src"])
            run(
                [
                    "git",
                    "-C",
                    str(source),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "--quiet",
                    "-m",
                    "test: stage source revision",
                ]
            )
            return run(["git", "-C", str(source), "rev-parse", "HEAD"])

        playbook = installation_play(root)
        variables = {
            "provider_authority_host_source_root": str(source),
            "provider_authority_host_uv_bin": shutil.which("uv"),
            "provider_authority_host_python": sys.executable,
        }

        def play(*, passes=True):
            result = subprocess.run(
                [
                    "ansible-playbook",
                    str(playbook),
                    "-i",
                    "localhost,",
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

        def assert_revision(expected):
            installed = run(
                [
                    str(root / "install/.venv/bin/python"),
                    "-I",
                    "-c",
                    "import kdive; print(kdive.REVISION)",
                ]
            )
            assert installed == expected, f"installed {installed!r}, expected {expected!r}"
            assert (root / "install/revision").read_text().strip() == revision
            assert (root / "restart").read_text().strip() == revision

        revision = commit()
        play()
        assert_revision("first")
        assert "changed=0 " in play()
        unchanged = (source / "pyproject.toml").stat().st_mtime_ns
        (package / "__init__.py").write_text('REVISION = "second"\n')
        revision = commit()
        assert (source / "pyproject.toml").stat().st_mtime_ns == unchanged
        play()
        assert_revision("second")
        assert "changed=0 " in play()
        # A corrupted installed project must not be attested or trigger a restart.
        installed = Path(
            run(
                [
                    str(root / "install/.venv/bin/python"),
                    "-I",
                    "-c",
                    "import kdive; print(kdive.__file__)",
                ]
            )
        )
        installed.write_text('REVISION = "corrupt"\n')
        marker_time = (root / "install/revision").stat().st_mtime_ns
        restart_time = (root / "restart").stat().st_mtime_ns
        play(passes=False)
        assert (root / "install/revision").stat().st_mtime_ns == marker_time
        assert (root / "restart").stat().st_mtime_ns == restart_time
        print(
            "authority_install: same-path source-only upgrade, exact bytes, restart, "
            "idempotence and failed proof passed"
        )


if __name__ == "__main__":
    main()
