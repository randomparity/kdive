"""Run production package tasks with real uv against a same-path source-only upgrade."""

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
HOOK_EQUIVALENT_ENV = "KDIVE_AUTHORITY_INSTALL_HOOK_EQUIVALENT"


def run(argv, **kwargs):
    result = subprocess.run(argv, text=True, capture_output=True, check=False, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def foreign_git_env():
    result = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    env = os.environ.copy()
    for name in result.stdout.splitlines():
        env.pop(name, None)
    return env


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


def run_fixture(env):
    with tempfile.TemporaryDirectory(prefix="kdive-authority-install-") as scratch:
        root = Path(scratch)
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
        env = env | {"UV_PYTHON_DOWNLOADS": "never", "UV_LINK_MODE": "copy"}
        run(["uv", "lock", "--project", str(source), "--python", sys.executable], env=env)
        run(["git", "init", "--quiet", str(source)], env=env)

        def commit():
            assert run(["git", "-C", str(source), "rev-parse", "--show-toplevel"], env=env) == str(
                source
            )
            run(["git", "-C", str(source), "add", "pyproject.toml", "uv.lock", "src"], env=env)
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
                ],
                env=env,
            )
            return run(["git", "-C", str(source), "rev-parse", "HEAD"], env=env)

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
                ],
                env=env,
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
                ],
                env=env,
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


def main():
    env = foreign_git_env()
    if os.environ.get(HOOK_EQUIVALENT_ENV) == "1":
        run_fixture(env)
        return

    with tempfile.TemporaryDirectory(prefix="kdive-authority-install-caller-") as scratch:
        caller = Path(scratch) / "caller"
        caller.mkdir()
        run(["git", "init", "--quiet", str(caller)], env=env)
        (caller / "caller.txt").write_text("caller\n")
        run(["git", "-C", str(caller), "add", "caller.txt"], env=env)
        run(
            [
                "git",
                "-C",
                str(caller),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "test: create hook caller",
            ],
            env=env,
        )
        config = caller / ".git/config"
        config_before = config.read_bytes()
        head_before = run(["git", "-C", str(caller), "rev-parse", "HEAD"], env=env)
        hook_env = env | {
            "GIT_DIR": str(caller / ".git"),
            "GIT_WORK_TREE": str(caller),
            "GIT_INDEX_FILE": str(caller / ".git/index"),
            HOOK_EQUIVALENT_ENV: "1",
        }
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve())],
            cwd=ROOT,
            env=hook_env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert config.read_bytes() == config_before
        assert run(["git", "-C", str(caller), "rev-parse", "HEAD"], env=env) == head_before
        assert run(["git", "-C", str(caller), "status", "--porcelain"], env=env) == ""


if __name__ == "__main__":
    main()
