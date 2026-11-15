"""The `test-linux` recipe drives Docker and macOS `podman machine` the same way (#3074).

A stub engine on `PATH` stands in for the container engine: it logs each call and answers
`info`, `build` and `run` as the test sets up, so no engine or VM is needed.
"""

import os
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_STUB_ENGINE = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG"
case "$1" in
  info) printf '%s\\n' "$STUB_SOCKET" ;;
  run) [[ " $* " == *" --entrypoint "* ]] && exit "$STUB_PROBE_EXIT" ;;
esac
exit 0
"""


def _run_recipe(
    tmp_path: Path, engine: str, *, probe_exit: int = 0
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run `just test-linux` against a stub engine; return the result and its logged calls."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / engine
    stub.write_text(_STUB_ENGINE, encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "calls.log"
    log.touch()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CONTAINER_ENGINE": engine,
        "STUB_LOG": str(log),
        "STUB_SOCKET": "unix:///run/user/501/podman/podman.sock",
        "STUB_PROBE_EXIT": str(probe_exit),
    }
    result = subprocess.run(
        ["just", "test-linux"],
        cwd=_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, log.read_text(encoding="utf-8").splitlines()


def _suite_runs(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("run ") and " --entrypoint " not in c]


def _probe_runs(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("run ") and " --entrypoint " in c]


def test_podman_mounts_the_socket_path_without_the_unix_scheme(tmp_path: Path) -> None:
    result, calls = _run_recipe(tmp_path, "podman")

    assert result.returncode == 0, result.stderr
    [suite] = _suite_runs(calls)
    assert "--volume /run/user/501/podman/podman.sock:/var/run/docker.sock" in suite
    assert "unix://" not in suite


def test_podman_disables_selinux_labels_and_docker_does_not(tmp_path: Path) -> None:
    podman_result, podman_calls = _run_recipe(tmp_path / "podman", "podman")
    docker_result, docker_calls = _run_recipe(tmp_path / "docker", "docker")

    assert podman_result.returncode == 0, podman_result.stderr
    assert docker_result.returncode == 0, docker_result.stderr
    for run in _suite_runs(podman_calls) + _probe_runs(podman_calls):
        assert "--security-opt label=disable" in run
    for run in _suite_runs(docker_calls) + _probe_runs(docker_calls):
        assert "--security-opt" not in run
    [docker_suite] = _suite_runs(docker_calls)
    assert "--volume /var/run/docker.sock:/var/run/docker.sock" in docker_suite


def test_unreadable_git_directory_stops_before_the_suite(tmp_path: Path) -> None:
    result, calls = _run_recipe(tmp_path, "podman", probe_exit=125)

    assert result.returncode == 1
    assert "test-linux: podman cannot read the git directory" in result.stderr
    assert "share" in result.stderr
    [probe] = _probe_runs(calls)
    assert ":/repo:ro" in probe
    assert _suite_runs(calls) == []


def test_readable_git_directory_runs_the_suite(tmp_path: Path) -> None:
    result, calls = _run_recipe(tmp_path, "docker")

    assert result.returncode == 0, result.stderr
    assert len(_probe_runs(calls)) == 1
    assert len(_suite_runs(calls)) == 1
