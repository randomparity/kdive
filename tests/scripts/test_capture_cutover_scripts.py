"""Executable safety proofs for the three protocol cutover authorities."""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_CUTOVER = ROOT / "scripts" / "cutover-capture-protocol-compose.sh"
HOST_CUTOVER = ROOT / "scripts" / "live-stack" / "cutover-capture-protocol.sh"
CHANGED_CUTOVER_SURFACE = (
    "scripts/cutover-capture-protocol-lib.sh",
    "scripts/live-stack/cutover-capture-protocol.sh",
    "scripts/cutover-capture-protocol-compose.sh",
    "scripts/cutover-capture-protocol-helm.sh",
    "deploy/helm/kdive/templates/statefulset-worker.yaml",
    "tests/scripts/test_capture_cutover_scripts.py",
    "tests/compose/test_compose_worker_lifecycle_live.py",
    "tests/helm/test_helm_upgrade_config.py",
    "tests/compose/test_compose_lifecycle_recipe.py",
)
UNTOUCHED_PYTHON_COMPLEXITY_BASELINE = {
    ("tests/compose/test_compose_worker_lifecycle_live.py", "_cleanup_isolated_stack"): 18,
}
DATABASE_PROCESS_LOG = """printf \
'%s argv=%s pgdatabase=%s pgpassfile=%s kdive=%s migration=%s\\n' \
  \"${0##*/}\" \"$*\" \"${PGDATABASE:-}\" \"${PGPASSFILE:-}\" \"${KDIVE_DATABASE_URL:-}\" \
  \"${KDIVE_MIGRATION_DATABASE_URL:-}\" >>\"$CUTOVER_TEST_LOG\"
"""
DATABASE_DUMP_TOOL = (
    DATABASE_PROCESS_LOG
    + """
for argument in "$@"; do
  case "$argument" in --file=*) output=${argument#--file=} ;; esac
done
printf 'valid custom dump' >"$output"
"""
)


def _executable(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}", encoding="utf-8")
    path.chmod(0o755)


def test_database_environment_scrub_covers_every_libpq_authority_variable() -> None:
    library = (ROOT / "scripts/cutover-capture-protocol-lib.sh").read_text(encoding="utf-8")
    scrub = library.split("cutover_scrub_database_environment()", maxsplit=1)[1].split(
        "}", maxsplit=1
    )[0]

    for variable in (
        "KDIVE_DATABASE_URL",
        "KDIVE_MIGRATION_DATABASE_URL",
        "PGPASSWORD",
        "PGSERVICE",
        "PGSERVICEFILE",
    ):
        assert variable in scrub


def _compose_stub_environment(
    tmp_path: Path,
    *,
    pull_status: int = 0,
    migrate_status: int = 0,
    start_status: int = 0,
    restore_list_status: int = 0,
    survivors: str = "",
    psql_body: str = "cat >/dev/null",
    host_database_identity: str = "kdive\\t16384\\t777",
    container_database_identity: str = "kdive\\t16384\\t777",
    switch_identity_after_stop: bool = False,
) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    switched_identity = "other\\t999\\t888"
    container_identity_command = f"printf '{container_database_identity}\\n'"
    host_identity_command = f"printf '{host_database_identity}\\n'"
    just_switch_command = ":"
    if switch_identity_after_stop:
        container_identity_command = (
            f"[[ -e \"$CUTOVER_DATABASE_SWITCH\" ]] && printf '{switched_identity}\\n' "
            f"|| printf '{container_database_identity}\\n'"
        )
        host_identity_command = (
            f"[[ -e \"$CUTOVER_DATABASE_SWITCH\" ]] && printf '{switched_identity}\\n' "
            f"|| printf '{host_database_identity}\\n'"
        )
        just_switch_command = (
            '[[ "$*" == "compose-stop" ]] && : >"$CUTOVER_DATABASE_SWITCH" || true'
        )
    _executable(
        bin_dir / "docker",
        f"""
printf 'docker %s kdive=%s migration=%s pgpassword=%s\n' "$*" \
  "${{KDIVE_DATABASE_URL:-}}" "${{KDIVE_MIGRATION_DATABASE_URL:-}}" \
  "${{PGPASSWORD:-}}" >>"$CUTOVER_TEST_LOG"
case "$*" in
  'image inspect --format '*' target:v3') printf 'sha256:abc123\n' ;;
  'image inspect '*) exit 1 ;;
  'pull '*) exit {pull_status} ;;
  *'config --format json'*)
    cat <<'JSON'
    {{"name":"cutover-proof","services":{{
  "migrate":{{"image":"target:v3"}},"server":{{"image":"target:v3"}},
  "worker":{{"image":"target:v3"}},"reconciler":{{"image":"target:v3"}}}}}}
JSON
    ;;
  *'run --rm --no-deps --entrypoint python migrate'*)
    {container_identity_command}
    ;;
  *'--profile cutover run --rm migrate'*) exit {migrate_status} ;;
  *'compose '*'ps '*'worker'*) printf '%s' {survivors!r} ;;
esac
""",
    )
    _executable(
        bin_dir / "just",
        f"""
printf 'just %s file=%s project=%s kdive=%s migration=%s pgpassword=%s\n' "$*" \
  "${{COMPOSE_FILE:-}}" "${{COMPOSE_PROJECT_NAME:-}}" \
  "${{KDIVE_DATABASE_URL:-}}" "${{KDIVE_MIGRATION_DATABASE_URL:-}}" \
  "${{PGPASSWORD:-}}" >>"$CUTOVER_TEST_LOG"
{just_switch_command}
[[ "$*" != "compose-up" ]] || exit {start_status}
""",
    )
    _executable(
        bin_dir / "psql",
        f"""
{DATABASE_PROCESS_LOG}
if [[ "$*" == *pg_control_system* ]]; then
  {host_identity_command}
else
  {psql_body}
fi
""",
    )
    _executable(bin_dir / "pg_dump", DATABASE_DUMP_TOOL)
    _executable(bin_dir / "pg_restore", f"exit {restore_list_status}\n")
    _executable(
        bin_dir / "gio",
        '[[ "${1:-}" == trash && -n "${2:-}" ]] && /usr/bin/unlink "$2" 2>/dev/null || true\n',
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CUTOVER_TEST_LOG": str(log),
        "CUTOVER_DATABASE_SWITCH": str(tmp_path / "database-switched"),
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


def _function_complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    decisions = 0
    decision_nodes = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.comprehension)
    for child in ast.walk(node):
        if isinstance(child, decision_nodes):
            decisions += 1
        elif isinstance(child, ast.BoolOp):
            decisions += len(child.values) - 1
        elif isinstance(child, ast.ExceptHandler):
            decisions += 1
        elif isinstance(child, ast.Match):
            decisions += max(0, len(child.cases) - 1)
    return decisions + 1


def _assert_line_limits(relative_files: tuple[str, ...]) -> None:
    for relative in relative_files:
        lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
        violations = [
            (number, len(line)) for number, line in enumerate(lines, 1) if len(line) > 100
        ]
        assert violations == [], f"{relative}: {violations}"


def _assert_python_quality(relative_files: tuple[str, ...]) -> None:
    for relative in (item for item in relative_files if item.endswith(".py")):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        oversized = {
            node.name: node.end_lineno - node.lineno + 1
            for node in functions
            if node.end_lineno is not None and node.end_lineno - node.lineno + 1 > 100
        }
        complex_functions = {
            node.name: _function_complexity(node)
            for node in functions
            if _function_complexity(node)
            > UNTOUCHED_PYTHON_COMPLEXITY_BASELINE.get((relative, node.name), 8)
        }
        assert oversized == {}, f"{relative}: {oversized}"
        assert complex_functions == {}, f"{relative}: {complex_functions}"


def _without_shell_heredocs(lines: list[str]) -> list[str]:
    body: list[str] = []
    marker: str | None = None
    for line in lines:
        if marker is not None:
            if line == marker:
                marker = None
            continue
        match = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
        if match is not None:
            marker = match.group(1)
        body.append(line)
    return body


def _shell_function_complexity(lines: list[str]) -> int:
    body = "\n".join(_without_shell_heredocs(lines))
    controls = re.findall(r"(?m)^\s*(?:if|elif|for|while|until|case)\b", body)
    boolean_branches = re.findall(r"(?:&&|\|\|)", body)
    case_branches = re.findall(r"(?m)^\s*(?!case\b)[^#\n]+\)\s+[^)]*$", body)
    return 1 + len(controls) + len(boolean_branches) + len(case_branches)


def _assert_shell_function_quality(relative_files: tuple[str, ...]) -> None:
    for relative in (item for item in relative_files if item.endswith(".sh")):
        lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
        for start, line in enumerate(lines):
            if not line.endswith("() {"):
                continue
            end = next(index for index in range(start + 1, len(lines)) if lines[index] == "}")
            function = lines[start : end + 1]
            complexity = _shell_function_complexity(function)
            assert len(function) <= 100, f"{relative}:{start + 1}-{end + 1}"
            assert complexity <= 8, f"{relative}:{start + 1} complexity={complexity}"


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


def test_backup_publish_race_never_overwrites_and_retains_validated_dump(
    tmp_path: Path,
) -> None:
    env, _log = _compose_stub_environment(tmp_path)
    destination = tmp_path / "backup.dump"
    pg_restore = tmp_path / "bin" / "pg_restore"
    _executable(
        pg_restore,
        f"printf 'unrelated' >{destination!s}\n",
    )

    result = _run_compose_cutover(tmp_path, env)

    assert result.returncode != 0
    assert destination.read_text(encoding="utf-8") == "unrelated"
    retained = list(tmp_path.glob("backup.dump.partial.*"))
    assert len(retained) == 1
    assert retained[0].read_text(encoding="utf-8") == "valid custom dump"
    assert "refusing to overwrite" in result.stderr


def test_compose_database_stall_times_out_before_stop_with_full_contract(tmp_path: Path) -> None:
    env, log = _compose_stub_environment(tmp_path, host_database_identity="")
    psql = tmp_path / "bin" / "psql"
    _executable(psql, "sleep 10\n")
    env["KDIVE_CUTOVER_OPERATION_TIMEOUT_SECONDS"] = "1"

    result = _run_compose_cutover(tmp_path, env)

    assert result.returncode == 124
    assert "monotonic clock" in result.stderr
    assert "one external operation" in result.stderr
    assert "incomplete result is rejected" in result.stderr
    assert "just compose-stop" not in log.read_text(encoding="utf-8")


def test_compose_database_identity_mismatch_fails_before_stop(tmp_path: Path) -> None:
    env, log = _compose_stub_environment(
        tmp_path,
        container_database_identity="other\\t999\\t888",
    )

    result = _run_compose_cutover(tmp_path, env)

    assert result.returncode != 0
    assert "database identities differ" in result.stderr
    assert "just compose-stop" not in log.read_text(encoding="utf-8")


def test_compose_database_identity_change_after_stop_refuses_backup(tmp_path: Path) -> None:
    env, log = _compose_stub_environment(tmp_path, switch_identity_after_stop=True)

    result = _run_compose_cutover(tmp_path, env)

    assert result.returncode != 0
    assert "approved database identity changed after stop" in result.stderr
    assert not (tmp_path / "backup.dump").exists()
    assert "--profile cutover run --rm migrate" not in log.read_text(encoding="utf-8")


def test_backup_publish_rejects_destination_symlink_race(tmp_path: Path) -> None:
    env, _log = _compose_stub_environment(tmp_path)
    destination = tmp_path / "backup.dump"
    redirect = tmp_path / "redirect"
    pg_restore = tmp_path / "bin" / "pg_restore"
    _executable(
        pg_restore,
        f"mkdir {redirect!s}\nln -s {redirect!s} {destination!s}\n",
    )

    result = _run_compose_cutover(tmp_path, env)

    assert result.returncode != 0
    assert destination.is_symlink()
    assert list(redirect.iterdir()) == []
    assert "refusing to overwrite" in result.stderr


def test_initial_broken_backup_symlink_fails_before_stop(tmp_path: Path) -> None:
    destination = tmp_path / "backup.dump"
    destination.symlink_to(tmp_path / "absent")
    env, log = _compose_stub_environment(tmp_path)

    result = _run_compose_cutover(tmp_path, env)

    assert result.returncode != 0
    assert not log.exists()


def test_compose_mutations_consume_only_frozen_project_and_model(tmp_path: Path) -> None:
    env, log = _compose_stub_environment(tmp_path)

    result = _run_compose_cutover(tmp_path, env)

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    mutation = next(
        index for index, line in enumerate(calls) if line.startswith("just compose-stop")
    )
    for line in calls[mutation:]:
        if line.startswith("docker ") and " compose " in line:
            assert "approved-compose.json" in line
            assert "--project-name cutover-proof" in line
        if line.startswith("just "):
            assert "approved-compose.json" in line
            assert "project=cutover-proof" in line


def test_compose_database_processes_never_receive_owner_dsn(tmp_path: Path) -> None:
    env, log = _compose_stub_environment(tmp_path)
    env["KDIVE_DATABASE_URL"] = (
        "postgresql://owner:argv-secret-%24%28x%29@db.example/kdive"  # pragma: allowlist secret
    )

    result = _run_compose_cutover(tmp_path, env)

    assert result.returncode == 0, result.stderr
    database_calls = "\n".join(
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith(("psql ", "pg_dump "))
    )
    assert "argv-secret" not in database_calls
    assert "pgpassfile=" in database_calls
    assert "pgdatabase=postgresql://owner@db.example/kdive" in database_calls


def test_compose_unrelated_children_never_inherit_owner_credentials(tmp_path: Path) -> None:
    env, log = _compose_stub_environment(tmp_path)
    env["KDIVE_DATABASE_URL"] = (
        "postgresql://owner:compose-child-secret@db.example/kdive"  # pragma: allowlist secret
    )
    env["PGPASSWORD"] = "ambient-compose-secret"  # pragma: allowlist secret

    result = _run_compose_cutover(tmp_path, env)

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert "compose-child-secret" not in calls
    assert "ambient-compose-secret" not in calls


def test_compose_rollback_recovery_uses_restricted_database_authority(tmp_path: Path) -> None:
    env, _log = _compose_stub_environment(tmp_path, start_status=37)
    env["KDIVE_DATABASE_URL"] = (
        "postgresql://owner:recovery-secret@db.example/kdive"  # pragma: allowlist secret
    )

    result = _run_compose_cutover(tmp_path, env)

    assert result.returncode == 37
    assert "recovery-secret" not in result.stdout + result.stderr
    assert "PGPASSFILE=" in result.stderr
    assert "postgresql://owner@db.example/kdive" in result.stderr


def _host_cutover_environment(
    tmp_path: Path,
    *,
    authority_status: int = 0,
    terminate_status: int = 0,
    migrate_status: int = 29,
) -> dict[str, str]:
    package = tmp_path / "package" / "kdive"
    lifecycle = package / "processes" / "lifecycle"
    lifecycle.mkdir(parents=True)
    for parent in (package, package / "processes", lifecycle):
        (parent / "__init__.py").write_text("", encoding="utf-8")
    main_source = """import os
import sys
import time
from pathlib import Path

log = Path(__import__("os").environ["CUTOVER_TEST_LOG"])
with log.open("a", encoding="utf-8") as stream:
    stream.write(
        f"kdive {sys.argv[1]} db={os.environ.get('KDIVE_DATABASE_URL', '')} "
        f"migration={os.environ.get('KDIVE_MIGRATION_DATABASE_URL', '')} "
        f"pgpassword={os.environ.get('PGPASSWORD', '')}\\n"
    )
if sys.argv[1] == "worker":
    while True:
        time.sleep(60)
if sys.argv[1] == "migrate":
    raise SystemExit(MIGRATE_STATUS)
""".replace("MIGRATE_STATUS", str(migrate_status))
    (package / "__main__.py").write_text(
        main_source,
        encoding="utf-8",
    )
    (lifecycle / "worker_incarnation.py").write_text(
        f"""import os
import sys
from pathlib import Path

action = sys.argv[1]
with Path(os.environ["CUTOVER_TEST_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(
        f"lifecycle {{action}} db={{os.environ.get('KDIVE_DATABASE_URL', '')}} "
        f"migration={{os.environ.get('KDIVE_MIGRATION_DATABASE_URL', '')}} "
        f"pgpassword={{os.environ.get('PGPASSWORD', '')}}\\n"
    )
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
printf 'pg_dump argv=%s pgdatabase=%s pgpassfile=%s kdive=%s migration=%s\n' \
  "$*" "${PGDATABASE:-}" "${PGPASSFILE:-}" "${KDIVE_DATABASE_URL:-}" \
  "${KDIVE_MIGRATION_DATABASE_URL:-}" >>"$CUTOVER_TEST_LOG"
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
    assert "restart the protocol-3 host processes exactly" in result.stderr
    assert "  restart_host_processes" in result.stderr
    calls = "\n".join(
        line
        for line in (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
        if not line.startswith("kdive worker ")
    )
    assert calls.index("check-local-cutover-authority") < calls.index("terminate-local-cutover")
    assert calls.index("terminate-local-cutover") < calls.index("kdive migrate")


def test_host_database_processes_never_receive_owner_dsn(tmp_path: Path) -> None:
    env = _host_cutover_environment(tmp_path)
    env["KDIVE_DATABASE_URL"] = (
        "postgresql://owner:host-argv-secret@db.example/kdive"  # pragma: allowlist secret
    )
    worker = _start_fake_host_worker(env)
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
    calls = "\n".join(
        line
        for line in (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
        if not line.startswith("kdive worker ")
    )
    assert "host-argv-secret" not in calls
    assert "pgpassfile=" in calls
    assert "pgdatabase=postgresql://owner@db.example/kdive" in calls


def test_host_python_children_receive_only_restricted_database_authority(tmp_path: Path) -> None:
    env = _host_cutover_environment(tmp_path)
    env["KDIVE_DATABASE_URL"] = (
        "postgresql://owner:host-child-secret@db.example/kdive"  # pragma: allowlist secret
    )
    env["PGPASSWORD"] = "ambient-host-secret"  # pragma: allowlist secret
    worker = _start_fake_host_worker(env)
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
    calls = "\n".join(
        line
        for line in (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
        if not line.startswith("kdive worker ")
    )
    assert "host-child-secret" not in calls
    assert "ambient-host-secret" not in calls
    assert "db=postgresql://owner@db.example/kdive" in calls


def test_host_rollback_recovery_uses_restricted_database_authority(tmp_path: Path) -> None:
    env = _host_cutover_environment(tmp_path, migrate_status=0)
    env["KDIVE_DATABASE_URL"] = (
        "postgresql://owner:host-recovery-secret@db.example/kdive"  # pragma: allowlist secret
    )
    env["KDIVE_WORKER_COUNT"] = "0"
    worker = _start_fake_host_worker(env)
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

    assert result.returncode != 0
    assert "host-recovery-secret" not in result.stdout + result.stderr
    assert "PGPASSFILE=" in result.stderr
    assert "postgresql://owner@db.example/kdive" in result.stderr


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


def test_host_recovery_command_quotes_hostile_backup_path(tmp_path: Path) -> None:
    marker = tmp_path / "substitution-ran"
    hostile = tmp_path / "backup-$(touch substitution-ran).dump"
    env = _host_cutover_environment(tmp_path, terminate_status=18)
    worker = _start_fake_host_worker(env)
    env["CUTOVER_TEST_WORKER_PID"] = str(worker.pid)
    try:
        result = subprocess.run(
            [str(HOST_CUTOVER), str(hostile)],
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

    command = next(
        line.strip() for line in result.stderr.splitlines() if line.startswith("  scripts/")
    )
    subprocess.run(["bash", "-c", command], cwd=tmp_path, check=False)
    assert not marker.exists()


def test_compose_recovery_command_quotes_hostile_backup_path(tmp_path: Path) -> None:
    marker = tmp_path / "compose-substitution-ran"
    hostile = tmp_path / "backup-$(touch compose-substitution-ran).dump"
    env, _log = _compose_stub_environment(tmp_path, restore_list_status=31)

    result = subprocess.run(
        [str(COMPOSE_CUTOVER), str(hostile), "target:v3"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    command = next(
        line.strip() for line in result.stderr.splitlines() if line.startswith("  scripts/")
    )
    subprocess.run(["bash", "-c", command], cwd=tmp_path, check=False)
    assert not marker.exists()


def test_compose_migration_recovery_prints_frozen_start_command(tmp_path: Path) -> None:
    env, _log = _compose_stub_environment(tmp_path, migrate_status=19)

    result = _run_compose_cutover(tmp_path, env)

    assert result.returncode == 19
    assert "approved-compose.json" in result.stderr
    assert "docker compose --profile cutover run --rm migrate" in result.stderr
    assert "just compose-up" in result.stderr


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
def test_cutover_scripts_use_restricted_database_rollback_authority(relative: str) -> None:
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert 'dbname=\\"${KDIVE_' not in text
    assert "cutover_print_restore_command" in text


def test_cutover_changed_surface_obeys_limits_with_scoped_python_baseline() -> None:
    _assert_line_limits(CHANGED_CUTOVER_SURFACE)
    _assert_python_quality(CHANGED_CUTOVER_SURFACE)
    _assert_shell_function_quality(CHANGED_CUTOVER_SURFACE)
