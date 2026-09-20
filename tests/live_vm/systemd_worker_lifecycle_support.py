"""Non-collected helpers for the real-host systemd worker proof."""

from __future__ import annotations

import json
import re
import subprocess
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[2]
ROLE_MEMBERS = {
    "kdive-server-member": "kdive_server",
    "kdive-worker-member": "kdive_worker",
    "kdive-reconciler-member": "kdive_reconciler",
    "kdive-witness-member": "kdive_lifecycle_witness",
}
_SYSTEMD_VALUE = re.compile(r"[A-Za-z0-9_.@:-]+")


def cgroup_populated(control_group: str, *, root: Path = Path("/sys/fs/cgroup")) -> bool:
    if not control_group:
        return False
    events = root / control_group.removeprefix("/") / "cgroup.events"
    try:
        lines = events.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False

    values: dict[str, str] = {}
    for line in lines:
        if not line:
            continue
        fields = line.split()
        if len(fields) != 2:
            raise ValueError(f"malformed cgroup.events record: {line!r}")
        key, value = fields
        if key in values:
            raise ValueError(f"duplicate cgroup.events record: {key}")
        values[key] = value
    populated = values.get("populated")
    if populated not in {"0", "1"}:
        raise ValueError("cgroup.events populated must be exactly 0 or 1")
    return populated == "1"


def assert_released_terminal_state(
    *, active_state: str, sub_state: str, result: str, exec_main_status: str
) -> None:
    """Require one of the runtime's supported empty-cgroup terminal shapes."""
    assert _SYSTEMD_VALUE.fullmatch(result), f"unsupported systemd Result={result}"
    status_is_valid = (
        exec_main_status.isascii()
        and exec_main_status.isdecimal()
        and len(exec_main_status) <= 3
        and int(exec_main_status, 10) in range(256)
    )
    assert status_is_valid, f"unsupported systemd ExecMainStatus={exec_main_status}"
    failed = active_state == "failed" and sub_state == "failed" and result != "success"
    retained_exit = active_state == "active" and sub_state == "exited" and result == "success"
    assert failed or retained_exit, (
        "unsupported released-cgroup state "
        f"ActiveState={active_state} SubState={sub_state} "
        f"Result={result} ExecMainStatus={exec_main_status}"
    )


def run(*argv: str, timeout: float = 130) -> str:
    result = subprocess.run(
        argv,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def docker_inspect(kind: str, object_name: str) -> dict[str, Any]:
    records = json.loads(run("docker", kind, "inspect", object_name))
    assert isinstance(records, list) and len(records) == 1
    record = records[0]
    assert isinstance(record, dict)
    return record


def assert_current_postgres_identity(container_id: str, container: dict[str, Any]) -> None:
    assert container["Id"] == container_id
    labels = container["Config"]["Labels"]
    assert labels["com.docker.compose.service"] == "postgres"
    assert Path(labels["com.docker.compose.project.working_dir"]).resolve() == ROOT
    config_files = labels["com.docker.compose.project.config_files"].split(",")
    assert (ROOT / "docker-compose.yml").resolve() in {
        Path(path).resolve() for path in config_files
    }


def assert_exact_runtime_roles(admin_dsn: str) -> None:
    with psycopg.connect(admin_dsn) as connection:
        rows = connection.execute(
            "SELECT member.rolname, member.rolcanlogin, member.rolinherit, "
            "member.rolsuper, member.rolcreatedb, member.rolcreaterole, "
            "member.rolreplication, member.rolbypassrls, "
            "COALESCE(array_agg(capability.rolname::text ORDER BY capability.rolname::text) "
            "FILTER (WHERE capability.rolname IS NOT NULL), ARRAY[]::text[]) "
            "FROM pg_roles AS member "
            "LEFT JOIN pg_auth_members AS membership ON membership.member = member.oid "
            "LEFT JOIN pg_roles AS capability ON capability.oid = membership.roleid "
            "WHERE member.rolname::text = ANY(%s) "
            "GROUP BY member.rolname, member.rolcanlogin, member.rolinherit, member.rolsuper, "
            "member.rolcreatedb, member.rolcreaterole, member.rolreplication, "
            "member.rolbypassrls",
            (list(ROLE_MEMBERS),),
        ).fetchall()
    actual = {row[0]: (*row[1:8], row[8]) for row in rows}
    assert actual == {
        member: (True, True, False, False, False, False, False, [capability])
        for member, capability in ROLE_MEMBERS.items()
    }


def wait_for_postgres(container_id: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while True:
        container = docker_inspect("container", container_id)
        assert_current_postgres_identity(container_id, container)
        state = container["State"]
        if state["Running"] and state.get("Health", {}).get("Status") == "healthy":
            return
        if time.monotonic() >= deadline:
            raise AssertionError("the current-flow PostgreSQL container did not recover")
        time.sleep(0.5)


def restore_postgres(container_id: str) -> None:
    container = docker_inspect("container", container_id)
    assert_current_postgres_identity(container_id, container)
    if not container["State"]["Running"]:
        assert run("docker", "start", container_id, timeout=30) == container_id
    wait_for_postgres(container_id)


def _retain_primary_failure(
    primary: BaseException | None, secondary: BaseException, operation: str
) -> BaseException:
    if primary is None:
        return secondary
    details = "".join(traceback.format_exception(secondary)).strip()
    primary.add_note(f"{operation} also raised:\n{details}")
    return primary


def recover_after_outage(
    failure: BaseException | None,
    *,
    restore_database: Callable[[], None],
    prove_retained_row: Callable[[], None],
    cleanup_workers: Callable[[], None],
) -> BaseException | None:
    try:
        restore_database()
    except BaseException as restore_error:
        return _retain_primary_failure(failure, restore_error, "PostgreSQL restoration failed")

    if failure is None:
        try:
            prove_retained_row()
        except BaseException as evidence_error:
            failure = _retain_primary_failure(None, evidence_error, "retained-row proof failed")

    try:
        cleanup_workers()
    except BaseException as cleanup_error:
        failure = _retain_primary_failure(failure, cleanup_error, "worker cleanup failed")
    return failure


def cleanup_after_case(
    *, restore_database: Callable[[], None], cleanup_workers: Callable[[], None]
) -> None:
    failure = recover_after_outage(
        None,
        restore_database=restore_database,
        prove_retained_row=lambda: None,
        cleanup_workers=cleanup_workers,
    )
    if failure is not None:
        raise failure.with_traceback(failure.__traceback__)
