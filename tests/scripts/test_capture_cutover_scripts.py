"""Executable safety proofs for the three protocol cutover authorities."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_CUTOVER = ROOT / "scripts" / "cutover-capture-protocol-compose.sh"
HOST_CUTOVER = ROOT / "scripts" / "live-stack" / "cutover-capture-protocol.sh"


def _executable(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}", encoding="utf-8")
    path.chmod(0o755)


def _compose_stub_environment(
    tmp_path: Path,
    *,
    pull_status: int = 0,
    migrate_status: int = 0,
    restore_list_status: int = 0,
    survivors: str = "",
    psql_body: str = "cat >/dev/null",
) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    _executable(
        bin_dir / "docker",
        f"""
printf 'docker %s\n' "$*" >>"$CUTOVER_TEST_LOG"
case "$*" in
  'image inspect '*) exit 1 ;;
  'pull '*) exit {pull_status} ;;
  *'config --format json'*)
    cat <<'JSON'
{{"services":{{"migrate":{{"image":"target:v3"}},"server":{{"image":"target:v3"}},
  "worker":{{"image":"target:v3"}},"reconciler":{{"image":"target:v3"}}}}}}
JSON
    ;;
  *'compose --profile cutover run --rm migrate'*) exit {migrate_status} ;;
  *'compose '*'ps '*'worker'*) printf '%s' {survivors!r} ;;
esac
""",
    )
    _executable(
        bin_dir / "just",
        'printf \'just %s\\n\' "$*" >>"$CUTOVER_TEST_LOG"\n',
    )
    _executable(bin_dir / "psql", f"{psql_body}\n")
    _executable(
        bin_dir / "pg_dump",
        """
for argument in "$@"; do
  case "$argument" in --file=*) output=${argument#--file=} ;; esac
done
printf 'valid custom dump' >"$output"
""",
    )
    _executable(bin_dir / "pg_restore", f"exit {restore_list_status}\n")
    _executable(
        bin_dir / "gio",
        '[[ "${1:-}" == trash && -n "${2:-}" ]] && /usr/bin/unlink "$2" 2>/dev/null || true\n',
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CUTOVER_TEST_LOG": str(log),
        "KDIVE_DATABASE_URL": "postgresql://u:sentinel-password@d/x",  # pragma: allowlist secret
        "KDIVE_CUTOVER_OPERATION_TIMEOUT_SECONDS": "3",
        "KDIVE_CUTOVER_DB_CONNECT_TIMEOUT_SECONDS": "1",
        "KDIVE_CUTOVER_DB_STATEMENT_TIMEOUT_SECONDS": "2",
    }
    return env, log


def _run_compose_cutover(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(COMPOSE_CUTOVER), str(tmp_path / "backup.dump"), "target:v3"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def test_compose_image_preflight_failure_does_not_stop_deployment(tmp_path: Path) -> None:
    env, log = _compose_stub_environment(tmp_path, pull_status=23)

    result = _run_compose_cutover(tmp_path, env)

    assert result.returncode == 23
    assert "pull target:v3" in log.read_text(encoding="utf-8")
    assert "just compose-stop" not in log.read_text(encoding="utf-8")
    assert "sentinel-password" not in result.stdout + result.stderr


def test_compose_migration_failure_redacts_dsn_and_proves_surviving_workers(
    tmp_path: Path,
) -> None:
    env, _log = _compose_stub_environment(
        tmp_path,
        migrate_status=19,
        survivors="deadbeef\\n",
    )

    result = _run_compose_cutover(tmp_path, env)

    assert result.returncode == 19
    output = result.stdout + result.stderr
    assert "sentinel-password" not in output
    assert "The named backup is complete" in output
    assert "workers may still be running" in output
    assert "deadbeef" in output


def test_compose_invalid_dump_is_removed_and_same_backup_path_is_resumable(
    tmp_path: Path,
) -> None:
    env, log = _compose_stub_environment(tmp_path, restore_list_status=31)

    result = _run_compose_cutover(tmp_path, env)

    assert result.returncode == 31
    assert not (tmp_path / "backup.dump").exists()
    assert not list(tmp_path.glob("backup.dump.partial.*"))
    assert "just compose-up" not in log.read_text(encoding="utf-8")
    assert "rerun the same command" in result.stderr


def test_compose_database_stall_times_out_before_stop_with_full_contract(tmp_path: Path) -> None:
    env, log = _compose_stub_environment(tmp_path, psql_body="sleep 10")
    env["KDIVE_CUTOVER_OPERATION_TIMEOUT_SECONDS"] = "1"

    result = _run_compose_cutover(tmp_path, env)

    assert result.returncode == 124
    assert "monotonic clock" in result.stderr
    assert "one external operation" in result.stderr
    assert "incomplete result is rejected" in result.stderr
    assert "just compose-stop" not in log.read_text(encoding="utf-8")


def _host_cutover_environment(
    tmp_path: Path, *, authority_status: int = 0, terminate_status: int = 0
) -> dict[str, str]:
    package = tmp_path / "package" / "kdive"
    lifecycle = package / "processes" / "lifecycle"
    lifecycle.mkdir(parents=True)
    for parent in (package, package / "processes", lifecycle):
        (parent / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        """import sys
import time
from pathlib import Path

log = Path(__import__("os").environ["CUTOVER_TEST_LOG"])
with log.open("a", encoding="utf-8") as stream:
    stream.write(f"kdive {sys.argv[1]}\\n")
if sys.argv[1] == "worker":
    while True:
        time.sleep(60)
if sys.argv[1] == "migrate":
    raise SystemExit(29)
""",
        encoding="utf-8",
    )
    (lifecycle / "worker_incarnation.py").write_text(
        f"""import os
import sys
from pathlib import Path

action = sys.argv[1]
with Path(os.environ["CUTOVER_TEST_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(f"lifecycle {{action}}\\n")
if action == "check-local-cutover-authority":
    if {authority_status}:
        print("worker process identity is unreadable", file=sys.stderr)
        raise SystemExit({authority_status})
elif action == "terminate-local-cutover":
    if {terminate_status}:
        print("PID was reused or was not stopped", file=sys.stderr)
        raise SystemExit({terminate_status})
""",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "psql", "cat >/dev/null\n")
    _executable(
        bin_dir / "pg_dump",
        """
for argument in "$@"; do
  case "$argument" in --file=*) output=${argument#--file=} ;; esac
done
printf 'valid custom dump' >"$output"
""",
    )
    _executable(bin_dir / "pg_restore", "exit 0\n")
    _executable(
        bin_dir / "gio",
        '[[ "${1:-}" == trash && -n "${2:-}" ]] && /usr/bin/unlink "$2" 2>/dev/null || true\n',
    )
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PYTHONPATH": str(tmp_path / "package"),
        "CUTOVER_TEST_LOG": str(tmp_path / "calls"),
        "KDIVE_PYTHON": str(ROOT / ".venv" / "bin" / "python"),
        "KDIVE_DATABASE_URL": "postgresql://u:host-sentinel@d/x",  # pragma: allowlist secret
        "KDIVE_CUTOVER_OPERATION_TIMEOUT_SECONDS": "3",
        "KDIVE_CUTOVER_DB_CONNECT_TIMEOUT_SECONDS": "1",
        "KDIVE_CUTOVER_DB_STATEMENT_TIMEOUT_SECONDS": "2",
    }


def _start_fake_host_worker(env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [env["KDIVE_PYTHON"], "-m", "kdive", "worker"],
        env=env,
        text=True,
    )


def test_host_cutover_executes_stop_witness_backup_and_migration_trap(tmp_path: Path) -> None:
    env = _host_cutover_environment(tmp_path)
    worker = _start_fake_host_worker(env)
    env["CUTOVER_TEST_WORKER_PID"] = str(worker.pid)
    try:
        result = subprocess.run(
            [str(HOST_CUTOVER), str(tmp_path / "backup.dump")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)

    assert result.returncode == 29
    assert "host-sentinel" not in result.stdout + result.stderr
    assert "host stopped-state proof found no KDIVE daemon" in result.stderr
    assert (tmp_path / "backup.dump").exists()
    calls = (tmp_path / "calls").read_text(encoding="utf-8")
    assert calls.index("check-local-cutover-authority") < calls.index("terminate-local-cutover")
    assert calls.index("terminate-local-cutover") < calls.index("kdive migrate")


def test_host_unreadable_process_authority_fails_before_stop_or_backup(tmp_path: Path) -> None:
    env = _host_cutover_environment(tmp_path, authority_status=17)
    worker = _start_fake_host_worker(env)
    env["CUTOVER_TEST_WORKER_PID"] = str(worker.pid)
    try:
        result = subprocess.run(
            [str(HOST_CUTOVER), str(tmp_path / "backup.dump")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert worker.poll() is None
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)

    assert result.returncode == 17
    assert "unreadable" in result.stderr
    assert not (tmp_path / "backup.dump").exists()


def test_host_pid_reuse_witness_failure_stays_stopped_without_backup(tmp_path: Path) -> None:
    env = _host_cutover_environment(tmp_path, terminate_status=18)
    worker = _start_fake_host_worker(env)
    env["CUTOVER_TEST_WORKER_PID"] = str(worker.pid)
    try:
        result = subprocess.run(
            [str(HOST_CUTOVER), str(tmp_path / "backup.dump")],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)

    assert result.returncode == 18
    assert "PID was reused" in result.stderr
    assert "host stopped-state proof found no KDIVE daemon" in result.stderr
    assert not (tmp_path / "backup.dump").exists()


def test_cutover_scripts_declare_bounded_database_and_operation_contracts() -> None:
    shared = (ROOT / "scripts/cutover-capture-protocol-lib.sh").read_text(encoding="utf-8")
    for relative in (
        "scripts/live-stack/cutover-capture-protocol.sh",
        "scripts/cutover-capture-protocol-compose.sh",
        "scripts/cutover-capture-protocol-helm.sh",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8") + shared
        assert "KDIVE_CUTOVER_OPERATION_TIMEOUT_SECONDS" in text
        assert "KDIVE_CUTOVER_DB_CONNECT_TIMEOUT_SECONDS" in text
        assert "KDIVE_CUTOVER_DB_STATEMENT_TIMEOUT_SECONDS" in text
        assert "monotonic" in text
        assert "pg_restore --list" in text
        assert ".partial." in text
        assert "workers may still be running" in text


@pytest.mark.parametrize(
    "relative",
    [
        "scripts/live-stack/cutover-capture-protocol.sh",
        "scripts/cutover-capture-protocol-compose.sh",
        "scripts/cutover-capture-protocol-helm.sh",
    ],
)
def test_cutover_scripts_never_expand_database_url_in_rollback(relative: str) -> None:
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert 'dbname=\\"${KDIVE_' not in text
