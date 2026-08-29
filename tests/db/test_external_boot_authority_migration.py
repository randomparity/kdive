"""Migration 0122 external-boot authority fences (ADR-0584)."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Event
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier, Literal
from psycopg.types.json import Jsonb

from tests.db_waits import wait_until_blocked_by

_LOGIN_PASSWORD = "external-boot-authority-test"  # pragma: allowlist secret
_PLAN = "sha256:" + "a" * 64
_JOURNAL = "sha256:" + "b" * 64
_QUIESCENCE = "sha256:" + "c" * 64
_EVIDENCE_DIGEST = "sha256:" + "d" * 64
_OBSERVED_AT = "2026-08-29T00:00:00+00:00"
_ALLOCATE_SIGNATURE = (
    "allocate_external_boot_authority(bytea,uuid,integer,uuid,uuid,uuid,text,text,text,text,text)"
)
_ACKNOWLEDGE_SIGNATURE = (
    "acknowledge_external_boot_authority(uuid,bigint,uuid,uuid,uuid,uuid,text,uuid,integer,"
    "text,text,text,text,text,text,text,bigint,text,text)"
)
_COMMIT_SIGNATURE = (
    "commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,"
    "text,text,text,text,text,text,bigint,text,jsonb)"
)


@dataclass(frozen=True, slots=True)
class _RoleDsns:
    parameters: dict[str, str]
    logins: dict[str, str]

    def __call__(self, role: str) -> str:
        parameters = {
            **self.parameters,
            "user": self.logins[role],
            "password": _LOGIN_PASSWORD,
        }
        return make_conninfo(**parameters)


@dataclass(frozen=True, slots=True)
class _AuthorityCase:
    allocation_id: UUID
    system_id: UUID
    run_id: UUID
    activation_id: UUID
    job_id: UUID
    attempt: int
    worker_id: str
    credential: bytes
    purpose: str
    provider_kind: str
    authority_instance: str
    operation: str
    operation_identity: str


@dataclass(frozen=True, slots=True)
class _Allocated:
    authority_id: UUID
    generation: int
    operation_digest: str


@pytest.fixture
def authority_role_dsns(migrated_url: str) -> Iterator[_RoleDsns]:
    """Create unique LOGIN principals for the migration's non-login roles."""
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        suffix = uuid4().hex[:16]
        logins = {
            role: f"kdive_eba_{role.removeprefix('kdive_')}_{suffix}"
            for role in (
                "kdive_server",
                "kdive_worker",
                "kdive_reconciler",
                "kdive_provider_authority",
            )
        }
        for role, login in logins.items():
            conn.execute(
                SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                    Identifier(login), Literal(_LOGIN_PASSWORD), Identifier(role)
                )
            )
        try:
            yield _RoleDsns(dict(conn.info.get_parameters()), logins)
        finally:
            for login in logins.values():
                conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(login)))


def _activation_evidence(system_id: UUID, run_id: UUID) -> tuple[Jsonb, Jsonb]:
    ownership = {"system_id": str(system_id), "run_id": str(run_id)}
    return (
        Jsonb(
            {
                "schema": "external-boot-materialization-v1",
                "ownership": ownership,
                "plan_identity": _PLAN,
            }
        ),
        Jsonb(
            {
                "schema": "external-boot-recovery-v1",
                "ownership": ownership,
                "plan_identity": _PLAN,
            }
        ),
    )


def _seed_case(
    conn: psycopg.Connection,
    *,
    purpose: str = "activate",
    operation: str | None = None,
    worker_protocol: int = 4,
    worker_suffix: str = "a",
) -> _AuthorityCase:
    resource_id, allocation_id, system_id = uuid4(), uuid4(), uuid4()
    investigation_id, run_id, activation_id, job_id = uuid4(), uuid4(), uuid4(), uuid4()
    worker_id = f"docker:external-authority-{worker_suffix}-{uuid4()}"
    credential = worker_suffix.encode() * 32
    provider_kind = "local-libvirt"
    authority_instance = f"authority-{worker_suffix}"
    operation = operation or purpose
    operation_identity = f"operation-{worker_suffix}-{uuid4()}"
    job_kind = "teardown" if purpose == "teardown" else "boot"

    conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'default', 'standard', 'available', 'qemu:///system')",
        (resource_id,),
    )
    conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'active', 'p', 'proj')",
        (allocation_id, resource_id),
    )
    conn.execute(
        "INSERT INTO systems (id, allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'p', 'proj')",
        (system_id, allocation_id, "failed" if purpose == "teardown" else "ready"),
    )
    conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'p', 'proj', 't', 'active')",
        (investigation_id,),
    )
    conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, build_profile, "
        "principal, project) VALUES (%s, %s, %s, 'local-libvirt', %s, '{}'::jsonb, 'p', 'proj')",
        (run_id, investigation_id, system_id, "failed" if purpose == "teardown" else "running"),
    )
    materialization, recovery_point = _activation_evidence(system_id, run_id)
    if purpose == "teardown":
        attempt_id = uuid4()
        conn.execute(
            "INSERT INTO external_boot_activations "
            "(id, system_id, run_id, plan_identity, operation_owner_id, authority_generation, "
            "state, materialization, pre_recovery_evidence, current_attempt_id) VALUES "
            "(%s, %s, %s, %s, %s, 1, 'recovery_failed', %s, %s, %s)",
            (
                activation_id,
                system_id,
                run_id,
                _PLAN,
                uuid4(),
                materialization,
                Jsonb(
                    {
                        "schema": "external-boot-pre-recovery-evidence-v1",
                        "activation_id": str(activation_id),
                        "system_id": str(system_id),
                        "run_id": str(run_id),
                        "plan_identity": _PLAN,
                    }
                ),
                attempt_id,
            ),
        )
        conn.execute(
            "INSERT INTO external_boot_recovery_attempts "
            "(activation_id, attempt_number, attempt_id, authority_generation, recovery_basis, "
            "state, terminal_evidence) VALUES (%s, 1, %s, 1, 'pre_recovery', 'failed', %s)",
            (
                activation_id,
                attempt_id,
                Jsonb(
                    {
                        "schema": "external-boot-terminal-evidence-v1",
                        "activation_id": str(activation_id),
                        "outcome": "recovery_failed",
                    }
                ),
            ),
        )
    else:
        conn.execute(
            "INSERT INTO external_boot_activations "
            "(id, system_id, run_id, plan_identity, operation_owner_id, authority_generation, "
            "state, materialization, recovery_point) "
            "VALUES (%s, %s, %s, %s, %s, 1, 'prepared', %s, %s)",
            (activation_id, system_id, run_id, _PLAN, uuid4(), materialization, recovery_point),
        )
    if worker_protocol < 4:
        conn.execute(
            "ALTER TABLE worker_incarnations DISABLE TRIGGER "
            "worker_incarnations_capture_protocol_floor"
        )
    try:
        conn.execute(
            "INSERT INTO worker_incarnations "
            "(incarnation, authority_kind, authority_binding, credential_hash, fence_protocol) "
            "VALUES (%s, 'docker', '{}'::jsonb, %s, %s)",
            (worker_id, credential, worker_protocol),
        )
    finally:
        if worker_protocol < 4:
            conn.execute(
                "ALTER TABLE worker_incarnations ENABLE TRIGGER "
                "worker_incarnations_capture_protocol_floor"
            )
    marker = {
        "activation_id": str(activation_id),
        "run_id": str(run_id),
        "system_id": str(system_id),
        "plan_identity": _PLAN,
        "purpose": purpose,
        "provider_kind": provider_kind,
        "authority_instance": authority_instance,
        "operation": operation,
        "operation_identity": operation_identity,
    }
    if worker_protocol < 4:
        conn.execute("ALTER TABLE jobs DISABLE TRIGGER jobs_current_worker_fence_protocol")
    try:
        conn.execute(
            "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
            "lease_expires_at, heartbeat_at, authorizing, dedup_key) VALUES "
            "(%s, %s, %s, 'running', 1, 3, %s, now() + interval '5 minutes', now(), %s, %s)",
            (
                job_id,
                job_kind,
                Jsonb({"external_boot_authority_v1": marker}),
                worker_id,
                Jsonb({"principal": "p", "project": "proj"}),
                f"external-authority-{job_id}",
            ),
        )
    finally:
        if worker_protocol < 4:
            conn.execute("ALTER TABLE jobs ENABLE TRIGGER jobs_current_worker_fence_protocol")
    return _AuthorityCase(
        allocation_id=allocation_id,
        system_id=system_id,
        run_id=run_id,
        activation_id=activation_id,
        job_id=job_id,
        attempt=1,
        worker_id=worker_id,
        credential=credential,
        purpose=purpose,
        provider_kind=provider_kind,
        authority_instance=authority_instance,
        operation=operation,
        operation_identity=operation_identity,
    )


def _allocate(worker: psycopg.Connection, case: _AuthorityCase) -> _Allocated:
    row = worker.execute(
        "SELECT status, authority_id, generation, operation_digest "
        "FROM allocate_external_boot_authority(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            case.credential,
            case.job_id,
            case.attempt,
            case.activation_id,
            case.run_id,
            case.system_id,
            _PLAN,
            case.purpose,
            case.provider_kind,
            case.authority_instance,
            case.operation_identity,
        ),
    ).fetchone()
    assert row is not None and row[0] == "allocated"
    return _Allocated(authority_id=row[1], generation=row[2], operation_digest=row[3])


def _prepare_purpose_state(conn: psycopg.Connection, case: _AuthorityCase, purpose: str) -> None:
    terminal = Jsonb(
        {
            "schema": "external-boot-terminal-evidence-v1",
            "activation_id": str(case.activation_id),
            "system_id": str(case.system_id),
            "outcome": "active" if purpose == "recover" else "abandoned",
        }
    )
    if purpose == "recover":
        conn.execute(
            "UPDATE external_boot_activations SET state='active', terminal_evidence=%s, "
            "activation_readiness_deadline=now() WHERE id=%s",
            (terminal, case.activation_id),
        )
    elif purpose == "resolve-conflict":
        attempt_id = uuid4()
        conn.execute(
            "INSERT INTO external_boot_recovery_attempts "
            "(activation_id, attempt_number, attempt_id, authority_generation, recovery_basis, "
            "state, conflict_evidence) VALUES (%s, 1, %s, 1, 'recovery_point', 'conflict', %s)",
            (
                case.activation_id,
                attempt_id,
                Jsonb(
                    {
                        "schema": "external-boot-conflict-evidence-v1",
                        "activation_id": str(case.activation_id),
                    }
                ),
            ),
        )
        conn.execute(
            "UPDATE external_boot_activations "
            "SET state='recovery_conflict', current_attempt_id=%s WHERE id=%s",
            (attempt_id, case.activation_id),
        )
    elif purpose == "release":
        conn.execute(
            "UPDATE external_boot_activations SET state='abandoned', materialization=NULL, "
            "recovery_point=NULL, terminal_evidence=%s WHERE id=%s",
            (terminal, case.activation_id),
        )


def _acknowledge(
    provider: psycopg.Connection,
    case: _AuthorityCase,
    authority: _Allocated,
    *,
    journal_digest: str = _JOURNAL,
    quiescence_digest: str = _QUIESCENCE,
) -> str:
    row = provider.execute(
        "SELECT status, journal_sequence, journal_digest, positive_quiescence_digest, "
        "acknowledged_at FROM acknowledge_external_boot_authority("
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            authority.authority_id,
            authority.generation,
            case.allocation_id,
            case.activation_id,
            case.run_id,
            case.system_id,
            _PLAN,
            case.job_id,
            case.attempt,
            case.purpose,
            case.provider_kind,
            case.authority_instance,
            case.worker_id,
            case.operation,
            case.operation_identity,
            authority.operation_digest,
            1,
            journal_digest,
            quiescence_digest,
        ),
    ).fetchone()
    assert row is not None
    if row[0] == "applied":
        assert row[1:] == (1, journal_digest, quiescence_digest, row[4])
        assert row[4] is not None
    else:
        assert row[1:] == (None, None, None, None)
    return row[0]


def _commit(
    worker: psycopg.Connection,
    case: _AuthorityCase,
    authority: _Allocated,
    result: dict[str, object],
    *,
    run_id: UUID | None = None,
) -> tuple[str, str | None]:
    row = worker.execute(
        "SELECT status, job_state FROM commit_external_boot_authority_result("
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            case.credential,
            case.job_id,
            case.attempt,
            authority.authority_id,
            authority.generation,
            case.activation_id,
            run_id or case.run_id,
            case.system_id,
            _PLAN,
            case.purpose,
            case.provider_kind,
            case.authority_instance,
            case.operation_identity,
            authority.operation_digest,
            1,
            _JOURNAL,
            Jsonb(result),
        ),
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def _terminal_evidence(case: _AuthorityCase, outcome: str) -> dict[str, object]:
    return {
        "schema": "external-boot-terminal-evidence-v1",
        "activation_id": str(case.activation_id),
        "system_id": str(case.system_id),
        "outcome": outcome,
        "composite_state": _EVIDENCE_DIGEST,
        "objects": [],
        "observed_at": _OBSERVED_AT,
    }


def _release_evidence(case: _AuthorityCase) -> dict[str, object]:
    return {
        "schema": "external-boot-release-evidence-v1",
        "activation_id": str(case.activation_id),
        "system_id": str(case.system_id),
        "store_identity": {"ref": "store"},
        "owner_key": {"ref": "owner"},
        "reserved_bytes": 4096,
        "enumeration_complete": True,
        "objects": [],
        "verified_at": _OBSERVED_AT,
    }


def _seed_release(conn: psycopg.Connection, case: _AuthorityCase) -> None:
    conn.execute(
        "INSERT INTO external_boot_reservation_releases "
        "(activation_id, store_identity, owner_key, reserved_bytes, release_identity, "
        "release_evidence) VALUES (%s, 'store', 'owner', 4096, %s, %s)",
        (case.activation_id, _EVIDENCE_DIGEST, Jsonb(_release_evidence(case))),
    )


def test_migration_creates_four_authority_tables_and_exact_role_grants(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
                "AND table_name LIKE 'external_boot_authorit%'"
            ).fetchall()
        }
        assert tables == {
            "external_boot_authority_counters",
            "external_boot_authorities",
            "external_boot_authority_acknowledgements",
            "external_boot_authority_audit",
        }
        allowed = {
            _ALLOCATE_SIGNATURE: {"kdive_worker"},
            _ACKNOWLEDGE_SIGNATURE: {"kdive_provider_authority"},
            _COMMIT_SIGNATURE: {"kdive_worker"},
        }
        for signature, roles in allowed.items():
            for role, login in authority_role_dsns.logins.items():
                assert conn.execute(
                    "SELECT has_function_privilege(%s, %s, 'EXECUTE')", (login, signature)
                ).fetchone() == (role in roles,)
        for role in ("kdive_worker", "kdive_reconciler", "kdive_provider_authority"):
            login = authority_role_dsns.logins[role]
            assert conn.execute(
                "SELECT has_table_privilege(%s, 'external_boot_authorities', 'UPDATE')", (login,)
            ).fetchone() == (False,)


def test_concurrent_allocations_are_strictly_ordered_per_system(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        first = _seed_case(conn, worker_suffix="d")
        second_job = uuid4()
        second_identity = f"operation-e-{uuid4()}"
        marker = conn.execute(
            "SELECT payload->'external_boot_authority_v1' FROM jobs WHERE id = %s",
            (first.job_id,),
        ).fetchone()
        assert marker is not None
        second_marker = dict(marker[0])
        second_marker["operation_identity"] = second_identity
        conn.execute(
            "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
            "lease_expires_at, heartbeat_at, authorizing, dedup_key) VALUES "
            "(%s, 'boot', %s, 'running', 1, 3, %s, now() + interval '5 minutes', now(), %s, %s)",
            (
                second_job,
                Jsonb({"external_boot_authority_v1": second_marker}),
                first.worker_id,
                Jsonb({"principal": "p", "project": "proj"}),
                str(second_job),
            ),
        )
    second = replace(first, job_id=second_job, operation_identity=second_identity)

    def allocate(case: _AuthorityCase) -> _Allocated:
        with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
            return _allocate(worker, case)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(allocate, case) for case in (first, second)]
        generations = sorted(future.result().generation for future in futures)
    assert generations == [1, 2]
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT state, count(*) FROM external_boot_authorities "
            "WHERE system_id = %s GROUP BY state ORDER BY state",
            (first.system_id,),
        ).fetchall() == [("allocating", 1), ("superseded", 1)]


def test_allocation_release_wins_before_a_waiting_authority_can_mint(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, worker_suffix="w")
    connected = Event()
    allocator_pid: list[int] = []

    def allocate() -> tuple[str] | None:
        with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
            allocator_pid.append(worker.info.backend_pid)
            connected.set()
            return worker.execute(
                "SELECT status FROM allocate_external_boot_authority("
                "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    case.credential,
                    case.job_id,
                    case.attempt,
                    case.activation_id,
                    case.run_id,
                    case.system_id,
                    _PLAN,
                    case.purpose,
                    case.provider_kind,
                    case.authority_instance,
                    case.operation_identity,
                ),
            ).fetchone()

    with (
        psycopg.connect(migrated_url) as releasing,
        psycopg.connect(migrated_url, autocommit=True) as observer,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        releasing.execute(
            "UPDATE allocations SET state='releasing' WHERE id=%s", (case.allocation_id,)
        )
        future = executor.submit(allocate)
        assert connected.wait(timeout=5)
        wait_until_blocked_by(
            observer,
            waiter_pid=allocator_pid[0],
            blocker_pid=releasing.info.backend_pid,
            future=future,
            expectation="authority allocation did not wait for the releasing Allocation row",
        )
        releasing.commit()
        assert future.result(timeout=5) == ("superseded",)
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute("SELECT count(*) FROM external_boot_authorities").fetchone() == (0,)


@pytest.mark.parametrize(
    ("purpose", "worker_suffix"),
    [
        ("activate", "p"),
        ("recover", "q"),
        ("resolve-conflict", "r"),
        ("release", "s"),
        ("teardown", "t"),
    ],
)
def test_every_purpose_maps_to_its_exact_running_job_kind(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    purpose: str,
    worker_suffix: str,
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, purpose=purpose, worker_suffix=worker_suffix)
        _prepare_purpose_state(conn, case, purpose)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert _allocate(worker, case).generation == 1


def test_later_run_cannot_reuse_an_earlier_activation_binding(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, worker_suffix="u")
        investigation_id = conn.execute(
            "SELECT investigation_id FROM runs WHERE id=%s", (case.run_id,)
        ).fetchone()
        assert investigation_id is not None
        later_run_id = uuid4()
        conn.execute(
            "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, "
            "build_profile, principal, project) VALUES "
            "(%s, %s, %s, 'local-libvirt', 'running', '{}'::jsonb, 'p', 'proj')",
            (later_run_id, investigation_id[0], case.system_id),
        )
        conn.execute(
            "UPDATE jobs SET payload=jsonb_set(payload, "
            "'{external_boot_authority_v1,run_id}', to_jsonb(%s::text)) WHERE id=%s",
            (str(later_run_id), case.job_id),
        )
    later = replace(case, run_id=later_run_id)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        row = worker.execute(
            "SELECT status FROM allocate_external_boot_authority(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                later.credential,
                later.job_id,
                later.attempt,
                later.activation_id,
                later.run_id,
                later.system_id,
                _PLAN,
                later.purpose,
                later.provider_kind,
                later.authority_instance,
                later.operation_identity,
            ),
        ).fetchone()
    assert row == ("superseded",)
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute("SELECT count(*) FROM external_boot_authorities").fetchone() == (0,)


def test_authority_binding_is_immutable_and_digest_is_database_minted(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, worker_suffix="f")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    assert authority.operation_digest.startswith("sha256:")
    assert len(authority.operation_digest) == 71
    with (
        psycopg.connect(migrated_url) as conn,
        pytest.raises(psycopg.errors.RaiseException, match="immutable"),
    ):
        conn.execute(
            "UPDATE external_boot_authorities SET operation_identity = 'replacement' WHERE id = %s",
            (authority.authority_id,),
        )


def test_acknowledgement_replay_and_delayed_takeover_are_fenced(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, worker_suffix="g")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        first = _allocate(worker, case)
    with psycopg.connect(authority_role_dsns("kdive_provider_authority"), autocommit=True) as host:
        assert _acknowledge(host, case, first) == "applied"
        assert _acknowledge(host, case, first) == "applied"

    with psycopg.connect(migrated_url) as conn:
        conn.execute("UPDATE jobs SET state = 'running' WHERE id = %s", (case.job_id,))
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        second = _allocate(worker, case)
    with psycopg.connect(authority_role_dsns("kdive_provider_authority"), autocommit=True) as host:
        assert _acknowledge(host, case, first) == "superseded"
        assert _acknowledge(host, case, second) == "applied"
        assert _acknowledge(host, case, second, journal_digest="sha256:" + "d" * 64) == (
            "superseded"
        )
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT journal_sequence, journal_digest, positive_quiescence_digest "
            "FROM external_boot_authority_acknowledgements WHERE authority_id = %s",
            (second.authority_id,),
        ).fetchone() == (1, _JOURNAL, _QUIESCENCE)


def test_acknowledgement_requires_bounded_positive_quiescence_before_writing(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, worker_suffix="h")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with (
        psycopg.connect(authority_role_dsns("kdive_provider_authority"), autocommit=True) as host,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        _acknowledge(host, case, authority, quiescence_digest="unproven")
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM external_boot_authority_acknowledgements"
        ).fetchone() == (0,)


def test_cross_binding_commit_is_superseded_without_durable_writes(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, worker_suffix="i")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with psycopg.connect(authority_role_dsns("kdive_provider_authority"), autocommit=True) as host:
        assert _acknowledge(host, case, authority) == "applied"
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert _commit(
            worker,
            case,
            authority,
            {"schema": "external-boot-authority-result-v1", "operation": "activate"},
            run_id=uuid4(),
        ) == ("superseded", None)
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute("SELECT state FROM jobs WHERE id = %s", (case.job_id,)).fetchone() == (
            "running",
        )
        assert conn.execute(
            "SELECT count(*) FROM external_boot_authority_audit WHERE outcome LIKE 'result_%'"
        ).fetchone() == (0,)


def test_cleanup_job_and_audit_commit_atomically(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, purpose="release", operation="cleanup", worker_suffix="j")
        conn.execute(
            "UPDATE external_boot_activations SET state = 'abandoned', materialization = NULL, "
            "recovery_point = NULL, terminal_evidence = %s WHERE id = %s",
            (
                Jsonb(
                    {
                        "schema": "external-boot-terminal-evidence-v1",
                        "activation_id": str(case.activation_id),
                        "system_id": str(case.system_id),
                        "outcome": "abandoned",
                    }
                ),
                case.activation_id,
            ),
        )
        _seed_release(conn, case)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with psycopg.connect(authority_role_dsns("kdive_provider_authority"), autocommit=True) as host:
        assert _acknowledge(host, case, authority) == "applied"
    evidence = {
        "schema": "external-boot-cleanup-evidence-v1",
        "activation_id": str(case.activation_id),
        "system_id": str(case.system_id),
        "release_identity": _EVIDENCE_DIGEST,
        "mode": "ordinary",
        "teardown_identity": None,
        "completed_at": _OBSERVED_AT,
    }
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert _commit(
            worker,
            case,
            authority,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "cleanup",
                "result_ref": "cleanup-result",
                "evidence": evidence,
            },
        ) == ("applied", "succeeded")
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT cleanup_complete, cleanup_evidence FROM external_boot_activations WHERE id=%s",
            (case.activation_id,),
        ).fetchone() == (True, evidence)
        assert conn.execute(
            "SELECT state, result_ref FROM jobs WHERE id = %s", (case.job_id,)
        ).fetchone() == ("succeeded", "cleanup-result")
        assert conn.execute(
            "SELECT count(*) FROM external_boot_authority_audit "
            "WHERE authority_id = %s AND outcome = 'result_committed'",
            (authority.authority_id,),
        ).fetchone() == (1,)


def test_semantically_incomplete_lifecycle_evidence_is_rejected_before_writes(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, purpose="release", operation="cleanup", worker_suffix="x")
        conn.execute(
            "UPDATE external_boot_activations SET state='abandoned', materialization=NULL, "
            "recovery_point=NULL, terminal_evidence=%s WHERE id=%s",
            (Jsonb(_terminal_evidence(case, "abandoned")), case.activation_id),
        )
        _seed_release(conn, case)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with psycopg.connect(authority_role_dsns("kdive_provider_authority"), autocommit=True) as host:
        assert _acknowledge(host, case, authority) == "applied"
    with (
        psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        _commit(
            worker,
            case,
            authority,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "cleanup",
                "evidence": {
                    "schema": "external-boot-cleanup-evidence-v1",
                    "activation_id": str(case.activation_id),
                    "system_id": str(case.system_id),
                    "mode": "ordinary",
                },
            },
        )
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT cleanup_complete FROM external_boot_activations WHERE id=%s",
            (case.activation_id,),
        ).fetchone() == (False,)
        assert conn.execute("SELECT state FROM jobs WHERE id=%s", (case.job_id,)).fetchone() == (
            "running",
        )
        assert conn.execute(
            "SELECT count(*) FROM external_boot_authority_audit "
            "WHERE authority_id=%s AND outcome LIKE 'result_%%'",
            (authority.authority_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("location", "field"),
    [
        pytest.param("result", "credential", id="credential-at-result-root"),
        pytest.param("evidence", "provider_secret", id="provider-secret-in-evidence"),
        pytest.param("object", "command", id="command-in-nested-object"),
        pytest.param("object", "path", id="path-in-nested-object"),
        pytest.param("object", "raw_definition", id="raw-definition-in-nested-object"),
        pytest.param("object", "unexpected", id="unknown-nested-object-field"),
        pytest.param("result", "unexpected", id="unknown-result-field"),
        pytest.param("evidence", "unexpected", id="unknown-evidence-field"),
        pytest.param("failure_context", "credential", id="credential-in-failure-context"),
        pytest.param("failure_context", "provider_secret", id="provider-secret-in-failure-context"),
        pytest.param("failure_context", "command", id="command-in-failure-context"),
        pytest.param("failure_context", "path", id="path-in-failure-context"),
        pytest.param("failure_context", "raw_definition", id="definition-in-failure-context"),
    ],
)
def test_unexpected_or_forbidden_result_content_is_rejected_without_writes(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    location: str,
    field: str,
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(
            conn,
            operation="fail" if location == "failure_context" else None,
            worker_suffix="u",
        )
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with psycopg.connect(authority_role_dsns("kdive_provider_authority"), autocommit=True) as host:
        assert _acknowledge(host, case, authority) == "applied"

    result: dict[str, object]
    if location == "failure_context":
        result = {
            "schema": "external-boot-authority-result-v1",
            "operation": "fail",
            "error_category": "infrastructure_failure",
            "failure_context": {field: "forbidden"},
            "terminal": True,
        }
    else:
        evidence = _terminal_evidence(case, "active")
        result = {
            "schema": "external-boot-authority-result-v1",
            "operation": "activate",
            "result_ref": "activate-result",
            "evidence": evidence,
            "activation_readiness_deadline": "2026-08-29T00:05:00+00:00",
        }
        if location == "result":
            result[field] = "forbidden"
        elif location == "evidence":
            evidence[field] = "forbidden"
        else:
            evidence["objects"] = [{"ref": "objects/a", field: "forbidden"}]

    with psycopg.connect(migrated_url) as conn:
        before = conn.execute(
            "SELECT e.state, e.terminal_evidence, e.activation_readiness_deadline, "
            "e.cleanup_complete, e.cleanup_evidence, e.teardown_evidence, j.state, "
            "j.result_ref, j.error_category, j.failure_context, r.state, r.failure_category, "
            "a.state, a.retired_at, (SELECT count(*) FROM external_boot_authority_audit "
            "WHERE authority_id=%s) FROM external_boot_activations AS e "
            "JOIN jobs AS j ON j.id=%s JOIN runs AS r ON r.id=%s "
            "JOIN external_boot_authorities AS a ON a.id=%s WHERE e.id=%s",
            (
                authority.authority_id,
                case.job_id,
                case.run_id,
                authority.authority_id,
                case.activation_id,
            ),
        ).fetchone()
    with (
        psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        _commit(worker, case, authority, result)
    with psycopg.connect(migrated_url) as conn:
        after = conn.execute(
            "SELECT e.state, e.terminal_evidence, e.activation_readiness_deadline, "
            "e.cleanup_complete, e.cleanup_evidence, e.teardown_evidence, j.state, "
            "j.result_ref, j.error_category, j.failure_context, r.state, r.failure_category, "
            "a.state, a.retired_at, (SELECT count(*) FROM external_boot_authority_audit "
            "WHERE authority_id=%s) FROM external_boot_activations AS e "
            "JOIN jobs AS j ON j.id=%s JOIN runs AS r ON r.id=%s "
            "JOIN external_boot_authorities AS a ON a.id=%s WHERE e.id=%s",
            (
                authority.authority_id,
                case.job_id,
                case.run_id,
                authority.authority_id,
                case.activation_id,
            ),
        ).fetchone()
    assert after == before


def test_recovery_attempt_creation_uses_locked_generation_and_next_sequence(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, purpose="recover", operation="recovery-attempt", worker_suffix="y")
        _prepare_purpose_state(conn, case, "recover")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with psycopg.connect(authority_role_dsns("kdive_provider_authority"), autocommit=True) as host:
        assert _acknowledge(host, case, authority) == "applied"
    attempt_id = uuid4()
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert _commit(
            worker,
            case,
            authority,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "recovery-attempt",
                "attempt_id": str(attempt_id),
                "recovery_basis": "recovery_point",
                "deadline": "2026-08-29T00:05:00+00:00",
            },
        ) == ("applied", "running")
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT attempt_number, attempt_id, authority_generation, state "
            "FROM external_boot_recovery_attempts WHERE activation_id=%s",
            (case.activation_id,),
        ).fetchone() == (1, attempt_id, authority.generation, "recovering")
        assert conn.execute(
            "SELECT state, current_attempt_id FROM external_boot_activations WHERE id=%s",
            (case.activation_id,),
        ).fetchone() == ("recovering", attempt_id)


def test_malformed_recovery_deadline_is_a_bounded_input_error_without_writes(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, purpose="recover", operation="recovery-attempt", worker_suffix="z")
        _prepare_purpose_state(conn, case, "recover")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with psycopg.connect(authority_role_dsns("kdive_provider_authority"), autocommit=True) as host:
        assert _acknowledge(host, case, authority) == "applied"
    with (
        psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        _commit(
            worker,
            case,
            authority,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "recovery-attempt",
                "attempt_id": str(uuid4()),
                "recovery_basis": "recovery_point",
                "deadline": "not-a-timestamp",
            },
        )
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM external_boot_recovery_attempts WHERE activation_id=%s",
            (case.activation_id,),
        ).fetchone() == (0,)
        assert conn.execute("SELECT state FROM jobs WHERE id=%s", (case.job_id,)).fetchone() == (
            "running",
        )


def test_retry_failure_requeues_job_and_audits_in_one_commit(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, operation="fail", worker_suffix="v")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with psycopg.connect(authority_role_dsns("kdive_provider_authority"), autocommit=True) as host:
        assert _acknowledge(host, case, authority) == "applied"
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert _commit(
            worker,
            case,
            authority,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "fail",
                "error_category": "infrastructure_failure",
                "failure_context": {"phase": "provider-call"},
                "terminal": False,
            },
        ) == ("applied", "queued")
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT state, worker_id, error_category, failure_context FROM jobs WHERE id=%s",
            (case.job_id,),
        ).fetchone() == ("queued", None, None, {})
        assert conn.execute(
            "SELECT outcome FROM external_boot_authority_audit "
            "WHERE authority_id=%s ORDER BY created_at DESC LIMIT 1",
            (authority.authority_id,),
        ).fetchone() == ("result_requeued",)


def test_teardown_is_terminal_only_inside_current_authority_commit(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, purpose="teardown", worker_suffix="k")
        _seed_release(conn, case)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with psycopg.connect(migrated_url) as conn:
        system = conn.execute(
            "SELECT state FROM systems WHERE id = %s", (case.system_id,)
        ).fetchone()
        assert system == ("failed",)
    with psycopg.connect(authority_role_dsns("kdive_provider_authority"), autocommit=True) as host:
        assert _acknowledge(host, case, authority) == "applied"
    teardown = {
        "schema": "external-boot-teardown-evidence-v1",
        "system_id": str(case.system_id),
        "system_state": "torn_down",
        "observed_at": _OBSERVED_AT,
    }
    cleanup = {
        "schema": "external-boot-cleanup-evidence-v1",
        "activation_id": str(case.activation_id),
        "system_id": str(case.system_id),
        "release_identity": _EVIDENCE_DIGEST,
        "mode": "system_teardown",
        "teardown_identity": _EVIDENCE_DIGEST,
        "completed_at": _OBSERVED_AT,
    }
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert _commit(
            worker,
            case,
            authority,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "teardown",
                "result_ref": "teardown-result",
                "teardown_evidence": teardown,
                "cleanup_evidence": cleanup,
            },
        ) == ("applied", "succeeded")
    with psycopg.connect(migrated_url) as conn:
        system = conn.execute(
            "SELECT state FROM systems WHERE id = %s", (case.system_id,)
        ).fetchone()
        assert system == ("torn_down",)
        assert conn.execute(
            "SELECT cleanup_complete, teardown_evidence FROM external_boot_activations WHERE id=%s",
            (case.activation_id,),
        ).fetchone() == (True, teardown)


def test_protocol_three_and_generic_external_job_paths_remain_denied(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        old = _seed_case(conn, worker_protocol=3, worker_suffix="l")
        current = _seed_case(conn, worker_suffix="m")
        ordinary_job = uuid4()
        conn.execute(
            "INSERT INTO jobs (id, kind, state, attempt, max_attempts, worker_id, payload, "
            "authorizing, dedup_key) VALUES "
            "(%s, 'boot', 'running', 1, 3, %s, '{}'::jsonb, '{}'::jsonb, %s)",
            (ordinary_job, current.worker_id, str(ordinary_job)),
        )
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert (
            worker.execute(
                "SELECT incarnation FROM authenticate_worker_incarnation(%s)", (old.credential,)
            ).fetchone()
            is None
        )
        assert worker.execute(
            "SELECT state FROM complete_worker_job(%s, %s, 1, 'ordinary')",
            (ordinary_job, current.credential),
        ).fetchone() == ("succeeded",)
        assert (
            worker.execute(
                "SELECT state FROM complete_worker_job(%s, %s, 1, 'forbidden')",
                (current.job_id, current.credential),
            ).fetchone()
            is None
        )
        assert (
            worker.execute(
                "SELECT state FROM complete_worker_job(%s, %s, 1, 'forbidden')",
                (old.job_id, old.credential),
            ).fetchone()
            is None
        )


def test_marked_jobs_are_not_claimable_and_no_readiness_switch_exists(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, worker_suffix="n")
        conn.execute(
            "UPDATE jobs SET state='queued', attempt=0, worker_id=NULL, lease_expires_at=NULL, "
            "heartbeat_at=NULL WHERE id=%s",
            (case.job_id,),
        )
        functions = conn.execute(
            "SELECT proname FROM pg_proc JOIN pg_namespace n ON n.oid=pronamespace "
            "WHERE n.nspname='public' AND proname LIKE '%external_boot%enable%'"
        ).fetchall()
        assert functions == []
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert (
            worker.execute(
                "SELECT id FROM claim_worker_job(%s, %s, interval '5 minutes', ARRAY['default'])",
                (case.worker_id, case.credential),
            ).fetchone()
            is None
        )


def test_audit_rows_exclude_credentials_provider_secrets_and_free_text(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, worker_suffix="o")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with psycopg.connect(authority_role_dsns("kdive_provider_authority"), autocommit=True) as host:
        assert _acknowledge(host, case, authority) == "applied"
    with psycopg.connect(migrated_url) as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='external_boot_authority_audit'"
            ).fetchall()
        }
        assert not columns & {
            "credential",
            "credential_hash",
            "provider_definition",
            "provider_secret",
            "command",
            "path",
            "message",
            "details",
        }
        serialized = conn.execute(
            "SELECT row_to_json(a)::text FROM external_boot_authority_audit AS a "
            "WHERE authority_id=%s ORDER BY created_at",
            (authority.authority_id,),
        ).fetchall()
        assert serialized
        assert case.credential.hex() not in "".join(row[0] for row in serialized)
