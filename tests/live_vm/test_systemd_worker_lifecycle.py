"""Real-host proof for retained systemd worker incarnations (ADR-0574)."""

from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest

from kdive.db.migrate import discover_migrations
from kdive.processes.lifecycle.systemd.systemd_worker_contract import LifecycleResponse, SlotPhase
from tests.live_vm import systemd_worker_lifecycle_support as support

pytestmark = pytest.mark.live_vm

_SOCKET = Path("/run/kdive/live-worker-lifecycle.sock")
_INSTALLED_PYTHON = Path("/opt/kdive-live-worker-lifecycle/.venv/bin/python")
_LIFECYCLE = support.ROOT / "scripts" / "live-stack" / "worker-lifecycle.sh"
_STATE_ROOT = Path("/var/lib/kdive/live-workers/slots")
_GATE_ENV = "KDIVE_RUN_SYSTEMD_WORKER_PROOF"


@dataclass(frozen=True, slots=True)
class ComposePostgres:
    """The exact PostgreSQL container and named volume owned by this Compose flow."""

    container_id: str
    volume_name: str


@dataclass(frozen=True, slots=True)
class ProofContext:
    """Validated real-host inputs shared by the lifecycle cases."""

    admin_dsn: str
    worker_dsn: str
    postgres: ComposePostgres


@dataclass(frozen=True, slots=True)
class IncarnationRow:
    """Database evidence for one exact worker incarnation."""

    incarnation: str
    binding: dict[str, str]
    credential_hash: bytes
    state: str
    outcome: str | None


@dataclass(frozen=True, slots=True)
class UnitEvidence:
    """Exact public systemd/cgroup evidence for one fixed worker unit."""

    unit: str
    invocation_id: str
    control_group: str
    active_state: str
    sub_state: str
    populated: bool


def _compose_postgres() -> ComposePostgres:
    container_id = support.run("docker", "compose", "ps", "-q", "postgres")
    assert container_id, "current Compose project has no PostgreSQL container"
    container = support.docker_inspect("container", container_id)
    support.assert_current_postgres_identity(container_id, container)
    labels = container["Config"]["Labels"]
    mounts = [
        mount for mount in container["Mounts"] if mount["Destination"] == "/var/lib/postgresql/data"
    ]
    assert len(mounts) == 1 and mounts[0]["Type"] == "volume"
    volume_name = mounts[0]["Name"]
    volume = support.docker_inspect("volume", volume_name)
    volume_labels = volume["Labels"]
    project = labels["com.docker.compose.project"]
    assert volume_labels["com.docker.compose.project"] == project
    assert volume_labels["com.docker.compose.volume"] == "kdive-pgdata"
    assert volume_name == f"{project}_kdive-pgdata"
    assert container["State"]["Health"]["Status"] == "healthy"
    return ComposePostgres(container_id=container_id, volume_name=volume_name)


def _assert_current_migrations(admin_dsn: str) -> None:
    expected = {
        migration.version: (migration.filename, migration.checksum)
        for migration in discover_migrations()
    }
    with psycopg.connect(admin_dsn) as connection:
        rows = connection.execute(
            "SELECT version, filename, checksum FROM schema_migrations"
        ).fetchall()
    actual = {version: (filename, checksum) for version, filename, checksum in rows}
    assert actual == expected


def _assert_prerequisites() -> ProofContext:
    assert _SOCKET.is_socket(), f"installed lifecycle socket is absent: {_SOCKET}"
    metadata = _SOCKET.stat()
    assert metadata.st_mode & 0o777 == 0o660
    assert _INSTALLED_PYTHON.is_file(), f"installed worker Python is absent: {_INSTALLED_PYTHON}"
    kernel_source = Path(os.environ.get("KDIVE_KERNEL_SRC", ""))
    assert kernel_source.is_absolute() and kernel_source.is_dir()
    assert kernel_source.resolve().is_relative_to(Path("/var/lib/kdive/build"))
    admin_dsn = os.environ.get("KDIVE_MIGRATION_DATABASE_URL", "")
    worker_dsn = os.environ.get("KDIVE_WORKER_DATABASE_URL", "")
    assert admin_dsn and worker_dsn, "live-stack role DSNs were not exported"
    postgres = _compose_postgres()
    _assert_current_migrations(admin_dsn)
    support.assert_exact_runtime_roles(admin_dsn)
    return ProofContext(admin_dsn=admin_dsn, worker_dsn=worker_dsn, postgres=postgres)


@pytest.fixture(scope="module")
def proof_context() -> ProofContext:
    gate = os.environ.get(_GATE_ENV)
    if gate is None:
        pytest.skip(f"{_GATE_ENV}=1 is required for the installed systemd proof")
    if gate != "1":
        pytest.fail(f"{_GATE_ENV} must be exactly 1 when present")
    try:
        context = _assert_prerequisites()
    except (AssertionError, KeyError, OSError, subprocess.SubprocessError) as exc:
        pytest.fail(f"systemd worker proof prerequisite failed: {exc}")
    response = _lifecycle("stop")
    assert response.ok, response.model_dump_json()
    return context


def _lifecycle(operation: str, count: int | None = None) -> LifecycleResponse:
    argv = [str(_LIFECYCLE), operation]
    if count is not None:
        argv.append(str(count))
    output = support.run(*argv)
    return LifecycleResponse.model_validate_json(output)


def _properties(unit: str) -> dict[str, str]:
    output = support.run(
        "systemctl",
        "show",
        "--property=ActiveState,SubState,ControlGroup,InvocationID",
        unit,
    )
    return dict(line.split("=", 1) for line in output.splitlines())


def _cgroup_populated(control_group: str) -> bool:
    events = Path("/sys/fs/cgroup") / control_group.removeprefix("/") / "cgroup.events"
    values = dict(line.split() for line in events.read_text(encoding="utf-8").splitlines())
    return values["populated"] == "1"


def _wait_for_empty_cgroup(control_group: str, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while _cgroup_populated(control_group):
        if time.monotonic() >= deadline:
            raise AssertionError(f"worker cgroup did not empty after SIGTERM: {control_group}")
        time.sleep(0.1)


def _unit_evidence(slot: int) -> UnitEvidence:
    unit = f"kdive-live-worker@{slot}.service"
    properties = _properties(unit)
    control_group = properties["ControlGroup"]
    return UnitEvidence(
        unit=unit,
        invocation_id=properties["InvocationID"],
        control_group=control_group,
        active_state=properties["ActiveState"],
        sub_state=properties["SubState"],
        populated=_cgroup_populated(control_group),
    )


def _active_rows(admin_dsn: str) -> list[IncarnationRow]:
    with psycopg.connect(admin_dsn) as connection:
        rows = connection.execute(
            "SELECT incarnation, authority_binding, credential_hash, state, outcome "
            "FROM worker_incarnations WHERE authority_kind = 'local' AND state = 'active' "
            "ORDER BY incarnation"
        ).fetchall()
    return [IncarnationRow(*row) for row in rows]


def _incarnation(admin_dsn: str, incarnation: str) -> IncarnationRow:
    with psycopg.connect(admin_dsn) as connection:
        row = connection.execute(
            "SELECT incarnation, authority_binding, credential_hash, state, outcome "
            "FROM worker_incarnations WHERE incarnation = %s",
            (incarnation,),
        ).fetchone()
    assert row is not None
    return IncarnationRow(*row)


def _wait_for_heartbeat(slot: int) -> None:
    port = 9465 if slot == 1 else 9468 + slot
    deadline = time.monotonic() + 20
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/livez", timeout=1) as reply:
                assert reply.status == 200
                assert reply.read() == b"ok"
                return
        except OSError, urllib.error.HTTPError:
            if time.monotonic() >= deadline:
                raise AssertionError(f"slot {slot} did not publish a live heartbeat") from None
            time.sleep(0.2)


def _assert_worker_login(context: ProofContext, expected_count: int) -> None:
    login = urlsplit(context.worker_dsn).username
    assert login
    with psycopg.connect(context.admin_dsn) as connection:
        row = connection.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = %s",
            (login,),
        ).fetchone()
    assert row is not None and row[0] >= expected_count


def _slot_artifacts_exist(slot: int) -> bool:
    slot_path = _STATE_ROOT / str(slot)
    result = subprocess.run(
        (
            "sudo",
            "test",
            "-e",
            str(slot_path / "state.json"),
            "-a",
            "-e",
            str(slot_path / "worker-incarnation.credential"),
        ),
        cwd=support.ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert not result.stdout and not result.stderr
    assert result.returncode in {0, 1}
    return result.returncode == 0


def _assert_started(context: ProofContext, count: int) -> tuple[IncarnationRow, ...]:
    response = _lifecycle("start", count)
    assert response.ok, response.model_dump_json()
    assert [(slot.slot, slot.unit, slot.phase) for slot in response.slots] == [
        (slot, f"kdive-live-worker@{slot}.service", SlotPhase.STARTED)
        for slot in range(1, count + 1)
    ]
    evidence = [_unit_evidence(slot) for slot in range(1, count + 1)]
    assert all(unit.active_state == "active" and unit.sub_state == "running" for unit in evidence)
    assert all(unit.populated for unit in evidence)
    rows = _active_rows(context.admin_dsn)
    assert len(rows) == count
    current = [row for row in rows if row.binding.get("unit") in {unit.unit for unit in evidence}]
    assert len(current) == count
    by_unit = {row.binding["unit"]: row for row in current}
    for unit in evidence:
        row = by_unit[unit.unit]
        assert row.binding["invocation_id"] == unit.invocation_id
        assert unit.control_group == f"/system.slice/{unit.unit}"
        slot = int(unit.unit.removeprefix("kdive-live-worker@").split(".")[0])
        assert _slot_artifacts_exist(slot)
        _wait_for_heartbeat(slot)
    _assert_worker_login(context, count)
    return tuple(current)


def _assert_stopped(context: ProofContext, rows: tuple[IncarnationRow, ...]) -> None:
    response = _lifecycle("stop")
    assert response.ok, response.model_dump_json()
    assert all(slot.phase is SlotPhase.TERMINATED for slot in response.slots)
    for row in rows:
        terminal = _incarnation(context.admin_dsn, row.incarnation)
        assert terminal.state == "terminated"
        assert terminal.outcome in {"succeeded", "failed", "killed"}
    assert not _lifecycle("status").slots
    for row in rows:
        properties = _properties(row.binding["unit"])
        assert properties["ActiveState"] == "inactive"
        assert properties["ControlGroup"] == ""
        assert properties["InvocationID"] == ""
        slot = int(row.binding["unit"].split("@")[1].split(".")[0])
        assert not _slot_artifacts_exist(slot)


@pytest.mark.parametrize("count", (1, 3))
def test_real_systemd_workers_register_heartbeat_and_terminate(
    proof_context: ProofContext, count: int
) -> None:
    rows: tuple[IncarnationRow, ...] = ()
    try:
        rows = _assert_started(proof_context, count)
        assert len({row.incarnation for row in rows}) == count
        assert len({row.binding["invocation_id"] for row in rows}) == count
        assert len({row.credential_hash for row in rows}) == count
    finally:
        if rows:
            _assert_stopped(proof_context, rows)
        else:
            _lifecycle("stop")
    if count == 3:
        _assert_partial_start_rollback_clears_released_first_slot(proof_context)


def _assert_retained_after_database_outage(
    context: ProofContext, before: UnitEvidence, row: IncarnationRow
) -> None:
    for operation in ("status", "stop"):
        response = _lifecycle(operation)
        assert not response.ok
        assert response.code == "dependency_unavailable"
        assert response.retry_action == "restore_database"
        assert [(slot.slot, slot.unit, slot.phase) for slot in response.slots] == [
            (1, before.unit, SlotPhase.STARTED)
        ]
    retained = _unit_evidence(1)
    assert retained.unit == before.unit
    assert retained.invocation_id == before.invocation_id
    assert retained.control_group == before.control_group
    assert retained.active_state == "active"
    assert retained.sub_state == "exited"
    assert not retained.populated
    assert _slot_artifacts_exist(1)
    assert row.binding["invocation_id"] == retained.invocation_id


def _assert_active_after_database_recovery(context: ProofContext, row: IncarnationRow) -> None:
    active = _incarnation(context.admin_dsn, row.incarnation)
    assert active.state == "active"
    assert active.credential_hash == row.credential_hash


def test_database_outage_retains_exact_invocation_until_stop_retry(
    proof_context: ProofContext,
) -> None:
    rows = _assert_started(proof_context, 1)
    row = rows[0]
    before = _unit_evidence(1)
    container_id = proof_context.postgres.container_id
    failure: BaseException | None = None
    try:
        assert support.run("docker", "stop", container_id, timeout=30) == container_id
        stopped_container = support.docker_inspect("container", container_id)
        support.assert_current_postgres_identity(container_id, stopped_container)
        assert stopped_container["State"]["Running"] is False
        support.run(
            "sudo",
            "systemctl",
            "kill",
            "--kill-whom=all",
            "--signal=SIGTERM",
            before.unit,
        )
        _wait_for_empty_cgroup(before.control_group)
        _assert_retained_after_database_outage(proof_context, before, row)
    except BaseException as outage_error:
        failure = outage_error
    finally:
        failure = support.recover_after_outage(
            failure,
            restore_database=lambda: support.restore_postgres(container_id),
            prove_retained_row=lambda: _assert_active_after_database_recovery(proof_context, row),
            cleanup_workers=lambda: _assert_stopped(proof_context, rows),
        )
    if failure is not None:
        raise failure.with_traceback(failure.__traceback__)
    current = support.docker_inspect("container", container_id)
    support.assert_current_postgres_identity(container_id, current)


def _lifecycle_result(operation: str, count: int | None = None) -> tuple[int, LifecycleResponse]:
    """Run one lifecycle operation that is expected to fail, and decode its response frame.

    ``support.run`` uses ``check=True``, but a refused or conflicting operation exits non-zero
    *after* printing the same bounded response frame a successful one prints. Reading it is the
    only way to assert the disposition rather than just the exit status.
    """
    argv = [str(_LIFECYCLE), operation]
    if count is not None:
        argv.append(str(count))
    result = subprocess.run(
        argv, cwd=support.ROOT, check=False, capture_output=True, text=True, timeout=130
    )
    return result.returncode, LifecycleResponse.model_validate_json(result.stdout.strip())


def _restart_out_of_band(unit: str, retained_invocation: str) -> str:
    """Restart one worker unit outside the lifecycle contract, as a sweep or an operator does.

    Under ADR-0657's strict gate binding the successor invocation exits non-zero, so `systemctl
    restart` itself fails; that failure is the wedge being created, not an error. This is the one
    precondition unit tests cannot build -- nothing in-process assigns a new `INVOCATION_ID`.
    """
    subprocess.run(
        ("sudo", "systemctl", "restart", unit),
        cwd=support.ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    properties = _properties(unit)
    successor = properties["InvocationID"]
    assert properties["ActiveState"] == "failed", properties
    assert len(successor) == 32 and successor != retained_invocation, properties
    return successor


def _reset_fleet() -> None:
    """Return every fixed unit to the inactive, empty-identity state the next test needs."""
    _lifecycle("stop")
    _lifecycle("recover")


def _assert_partial_start_rollback_clears_released_first_slot(proof_context: ProofContext) -> None:
    """A rejected second slot must not leave the first slot's released cgroup retained."""
    unit = "kdive-live-worker@2.service"
    drop_in = Path(f"/run/systemd/system/{unit}.d/kdive-live-proof-2596.conf")
    preflight = subprocess.run(
        ("sudo", "test", "!", "-e", str(drop_in)),
        cwd=support.ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert preflight.returncode == 0

    try:
        installed = subprocess.run(
            (
                "sudo",
                "systemctl",
                "edit",
                "--runtime",
                "--drop-in=kdive-live-proof-2596.conf",
                "--stdin",
                unit,
            ),
            cwd=support.ROOT,
            check=False,
            capture_output=True,
            input="[Service]\nExecStart=\nExecStart=/bin/false\n",
            text=True,
            timeout=30,
        )
        assert installed.returncode == 0

        status, response = _lifecycle_result("start", 2)
        assert status == 4 and not response.ok
        assert response.code == "conflict" and response.retry_action == "operator_recovery"
    finally:
        subprocess.run(
            ("sudo", "rm", "-f", str(drop_in)),
            cwd=support.ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        subprocess.run(
            ("sudo", "systemctl", "daemon-reload"),
            cwd=support.ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        _reset_fleet()
        assert not _active_rows(proof_context.admin_dsn)
        for slot in (1, 2):
            properties = _properties(f"kdive-live-worker@{slot}.service")
            assert properties["ActiveState"] == "inactive"
            assert properties["ControlGroup"] == ""
            assert properties["InvocationID"] == ""
            assert not _slot_artifacts_exist(slot)
        removed = subprocess.run(
            ("sudo", "test", "!", "-e", str(drop_in)),
            cwd=support.ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert removed.returncode == 0


def test_recover_clears_the_failed_identity_that_blocks_the_next_start(
    proof_context: ProofContext,
) -> None:
    """The residual ADR-0657 discloses and nothing shipped could reach.

    `stop` retires the restarted slot's binding and removes its files, but `systemctl stop` is a
    no-op on the `failed` unit it leaves, so the retained `InvocationID` survives and
    `require_inactive` refuses the next `start`. With `state.json` gone, every later `stop` skips
    the slot entirely, so before `recover` there was no operation that could clear it.
    """
    rows = _assert_started(proof_context, 1)
    try:
        before = _unit_evidence(1)
        _restart_out_of_band(before.unit, before.invocation_id)

        assert _lifecycle("stop").ok
        retired = _incarnation(proof_context.admin_dsn, rows[0].incarnation)
        assert retired.state == "terminated" and retired.outcome == "killed"
        assert not _slot_artifacts_exist(1)

        # The wedge: the unit still holds an identity, and that alone refuses activation.
        wedged = _properties(before.unit)
        assert wedged["ActiveState"] == "failed" and wedged["InvocationID"] != ""
        status, refused = _lifecycle_result("start", 1)
        assert status == 4 and not refused.ok
        assert refused.code == "conflict" and refused.retry_action == "operator_recovery"

        recovered = _lifecycle("recover")
        assert recovered.ok, recovered.model_dump_json()
        assert [(slot.slot, slot.message) for slot in recovered.slots] == [
            (1, "cleared the retained unit identity")
        ]
        cleared = _properties(before.unit)
        assert cleared["ActiveState"] == "inactive"
        assert cleared["SubState"] == "dead"
        assert cleared["InvocationID"] == ""

        # The point of clearing it: the slot is startable again with no hand-run reset-failed.
        restarted = _assert_started(proof_context, 1)
        assert restarted[0].binding["invocation_id"] != before.invocation_id
    finally:
        _reset_fleet()


def test_recover_retires_a_restarted_slot_and_releases_its_fence_in_one_call(
    proof_context: ProofContext,
) -> None:
    """The other branch: retained facts still present, so one call clears facts, fence and unit."""
    rows = _assert_started(proof_context, 1)
    try:
        before = _unit_evidence(1)
        successor = _restart_out_of_band(before.unit, before.invocation_id)

        response = _lifecycle("recover")
        assert response.ok, response.model_dump_json()
        assert [(slot.slot, slot.phase) for slot in response.slots] == [(1, SlotPhase.TERMINATED)]

        retired = _incarnation(proof_context.admin_dsn, rows[0].incarnation)
        assert retired.state == "terminated" and retired.outcome == "killed"
        # ADR-0657: evidence is published for the retained binding, never the successor's.
        assert retired.binding["invocation_id"] == before.invocation_id != successor
        assert not _slot_artifacts_exist(1)
        cleared = _properties(before.unit)
        assert cleared["ActiveState"] == "inactive" and cleared["InvocationID"] == ""
    finally:
        _reset_fleet()


def test_recover_refuses_a_live_slot_without_releasing_its_fence(
    proof_context: ProofContext,
) -> None:
    """The guard that matters most: recovery must never retire a worker that is still running."""
    rows = _assert_started(proof_context, 1)
    try:
        before = _unit_evidence(1)
        assert before.populated

        status, response = _lifecycle_result("recover")
        assert status == 4 and not response.ok
        assert response.code == "conflict" and response.retry_action == "operator_recovery"
        assert [(slot.slot, slot.code) for slot in response.slots] == [(1, "recovery_refused")]

        untouched = _incarnation(proof_context.admin_dsn, rows[0].incarnation)
        assert untouched.state == "active" and untouched.outcome is None
        assert _slot_artifacts_exist(1)
        after = _unit_evidence(1)
        assert after.invocation_id == before.invocation_id and after.populated
        _wait_for_heartbeat(1)
    finally:
        _reset_fleet()
