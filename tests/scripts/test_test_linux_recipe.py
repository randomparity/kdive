"""The `test-linux` recipe drives Docker and macOS `podman machine` the same way (#3074).

A stub engine on `PATH` stands in for the container engine: it logs each call and answers
`info` (socket path and memory), `build` and `run` as the test sets up, so no engine or VM
is needed.
"""

import os
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

_STUB_ENGINE = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG"
case "$1" in
  info)
    case "$*" in
      *MemTotal*) printf '%s\\n' "$STUB_MEMORY" ;;
      *) printf '%s\\n' "$STUB_SOCKET" ;;
    esac
    ;;
  run) [[ " $* " == *" --entrypoint "* ]] && exit "$STUB_PROBE_EXIT" ;;
esac
exit 0
"""


def _run_recipe(
    tmp_path: Path,
    engine: str,
    *args: str,
    probe_exit: int = 0,
    memory: str = str(32 * 1024**3),
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
        "STUB_MEMORY": memory,
    }
    result = subprocess.run(
        ["just", "test-linux", "HEAD", *args],
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


def _suite_args(calls: list[str]) -> list[str]:
    """The arguments the suite container's entry point receives, after the commit."""
    [suite] = _suite_runs(calls)
    return suite.split(" kdive-test-linux ", maxsplit=1)[1].split()[1:]


def test_worker_cap_fits_a_small_engine_vm(tmp_path: Path) -> None:
    result, calls = _run_recipe(tmp_path, "podman", memory=str(3782492160))

    assert result.returncode == 0, result.stderr
    assert _suite_args(calls) == ["--maxprocesses=2"]
    assert "podman has 3607 MiB; at most 2 xdist workers" in result.stderr


def test_worker_cap_stays_at_eight_with_enough_memory(tmp_path: Path) -> None:
    result, calls = _run_recipe(tmp_path, "docker", memory=str(33596157952))

    assert result.returncode == 0, result.stderr
    assert _suite_args(calls) == ["--maxprocesses=8"]


def test_worker_cap_never_drops_below_one(tmp_path: Path) -> None:
    result, calls = _run_recipe(tmp_path, "podman", memory=str(1024**3))

    assert result.returncode == 0, result.stderr
    assert _suite_args(calls) == ["--maxprocesses=1"]


def test_unreadable_memory_keeps_eight_workers_with_a_warning(tmp_path: Path) -> None:
    result, calls = _run_recipe(tmp_path, "podman", memory="<no value>")

    assert result.returncode == 0, result.stderr
    assert _suite_args(calls) == ["--maxprocesses=8"]
    assert "warning: cannot read the podman memory" in result.stderr


@pytest.mark.parametrize(
    "worker_args",
    [["-n", "6"], ["-n6"], ["--numprocesses=6"], ["--maxprocesses=6"], ["--maxprocesses", "6"]],
)
def test_explicit_worker_count_replaces_the_memory_cap(
    tmp_path: Path, worker_args: list[str]
) -> None:
    result, calls = _run_recipe(
        tmp_path, "podman", *worker_args, "tests/jobs", memory=str(3782492160)
    )

    assert result.returncode == 0, result.stderr
    assert _suite_args(calls) == [*worker_args, "tests/jobs"]
    assert "the memory cap is not used" in result.stderr


def test_other_arguments_keep_the_memory_cap(tmp_path: Path) -> None:
    result, calls = _run_recipe(tmp_path, "podman", "-k", "retry", memory=str(3782492160))

    assert result.returncode == 0, result.stderr
    assert _suite_args(calls) == ["--maxprocesses=2", "-k", "retry"]
