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


def run(argv, **kwargs):
    result = subprocess.run(argv, text=True, capture_output=True, check=False, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def installation_play(root):
    tasks = yaml.safe_load(
        (HERE.parent / "roles/provider_authority_host/tasks/install.yml").read_text()
    )
    start = next(i for i, task in enumerate(tasks) if "frozen dependency lock" in task["name"])
    # Include the marker read that the production command uses, when present.
    while start and "revision" in tasks[start - 1]["name"].lower():
        start -= 1
    end = next(i for i, task in enumerate(tasks) if "database and TLS material" in task["name"])
    selected = tasks[1:3] + tasks[start:end]
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


def main():
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
