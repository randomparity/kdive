"""Database authority boundaries for worker-incarnation artifact fences."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from threading import Event
from time import monotonic, sleep
from typing import LiteralString
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier, Literal
from psycopg.types.json import Jsonb

from kdive.db import migrate
from kdive.services.runs.worker_incarnations import CURRENT_WORKER_FENCE_PROTOCOL

_LOGIN_AUTHENTICATION = "worker-fence-test-authentication"
_BINDING_MAX_BYTES = 4096
_PROTECTED_TABLES = {
    "investigation_build_use_recoveries",
    "investigation_build_uses",
    "schema_migrations",
    "worker_incarnations",
}
_ORDINARY_TABLES = {
    "allocations",
    "artifacts",
    "audit_log",
    "budgets",
    "build_artifact_gc_cursors",
    "component_uploads",
    "cost_class_coefficients",
    "debug_sessions",
    "egress_probe_guests",
    "idempotency_keys",
    "image_catalog",
    "investigation_build_gc_cursor",
    "investigation_build_tombstones",
    "investigation_builds",
    "investigations",
    "inventory_overrides",
    "jobs",
    "ledger",
    "object_write_leases",
    "ops_control",
    "platform_audit_log",
    "provider_components",
    "quotas",
    "resources",
    "rootfs_fetch_leases",
    "run_steps",
    "runs",
    "snapshots",
    "system_bootstrap_keys",
    "system_object_sweep_cursors",
    "system_shapes",
    "systems",
    "tool_invocation",
    "upload_manifests",
}
_SERVER_MUTATIONS = {
    "INSERT": _ORDINARY_TABLES
    - {
        "build_artifact_gc_cursors",
        "investigation_build_gc_cursor",
        "investigation_build_tombstones",
        "system_object_sweep_cursors",
    },
    "UPDATE": _ORDINARY_TABLES
    - {
        "audit_log",
        "build_artifact_gc_cursors",
        "investigation_build_gc_cursor",
        "investigation_build_tombstones",
        "ledger",
        "platform_audit_log",
        "system_object_sweep_cursors",
        "tool_invocation",
    },
    "DELETE": {
        "artifacts",
        "component_uploads",
        "debug_sessions",
        "egress_probe_guests",
        "idempotency_keys",
        "image_catalog",
        "inventory_overrides",
        "provider_components",
        "resources",
        "rootfs_fetch_leases",
        "run_steps",
        "snapshots",
        "system_bootstrap_keys",
        "system_shapes",
        "tool_invocation",
        "upload_manifests",
    },
}
_WORKER_SELECT = {
    "allocations",
    "artifacts",
    "budgets",
    "component_uploads",
    "cost_class_coefficients",
    "debug_sessions",
    "egress_probe_guests",
    "image_catalog",
    "investigation_build_tombstones",
    "investigation_builds",
    "investigations",
    "jobs",
    "ledger",
    "object_write_leases",
    "ops_control",
    "provider_components",
    "quotas",
    "resources",
    "rootfs_fetch_leases",
    "run_steps",
    "runs",
    "snapshots",
    "system_bootstrap_keys",
    "system_shapes",
    "systems",
    "upload_manifests",
}
_WORKER_MUTATIONS = {
    "INSERT": {
        "artifacts",
        "component_uploads",
        "egress_probe_guests",
        "ledger",
        "object_write_leases",
        "rootfs_fetch_leases",
        "run_steps",
        "snapshots",
        "upload_manifests",
    },
    "UPDATE": {
        "allocations",
        "artifacts",
        "budgets",
        "component_uploads",
        "debug_sessions",
        "egress_probe_guests",
        "image_catalog",
        "investigation_builds",
        "investigations",
        "run_steps",
        "runs",
        "snapshots",
        "systems",
        "upload_manifests",
    },
    "DELETE": {
        "artifacts",
        "object_write_leases",
        "rootfs_fetch_leases",
        "run_steps",
        "snapshots",
        "system_bootstrap_keys",
        "upload_manifests",
    },
}
_RECONCILER_SELECT = _ORDINARY_TABLES - {"audit_log", "platform_audit_log", "tool_invocation"}
_RECONCILER_MUTATIONS = {
    "INSERT": {
        "artifacts",
        "cost_class_coefficients",
        "image_catalog",
        "investigation_build_tombstones",
        "inventory_overrides",
        "jobs",
        "ledger",
        "resources",
    },
    "UPDATE": {
        "allocations",
        "budgets",
        "build_artifact_gc_cursors",
        "cost_class_coefficients",
        "debug_sessions",
        "egress_probe_guests",
        "image_catalog",
        "investigation_build_gc_cursor",
        "investigation_builds",
        "investigations",
        "jobs",
        "resources",
        "runs",
        "snapshots",
        "system_object_sweep_cursors",
        "systems",
        "upload_manifests",
    },
    "DELETE": {
        "artifacts",
        "idempotency_keys",
        "image_catalog",
        "investigation_builds",
        "inventory_overrides",
        "jobs",
        "object_write_leases",
        "resources",
        "rootfs_fetch_leases",
        "run_steps",
        "snapshots",
        "system_bootstrap_keys",
        "upload_manifests",
    },
}
_EXPECTED_ROLE_TABLE_PRIVILEGES = {
    "kdive_server": {"SELECT": _ORDINARY_TABLES, **_SERVER_MUTATIONS},
    "kdive_worker": {"SELECT": _WORKER_SELECT, **_WORKER_MUTATIONS},
    "kdive_reconciler": {"SELECT": _RECONCILER_SELECT, **_RECONCILER_MUTATIONS},
    "kdive_lifecycle_witness": {},
    "unprivileged": {},
}


@dataclass(frozen=True)
class RoleDsns:
    """Connection strings and LOGIN names for the isolated runtime principals."""

    parameters: dict[str, str]
    logins: dict[str, str]

    def __call__(self, role: str) -> str:
        parameters = {
            **self.parameters,
            "user": self.logins[role],
            "password": _LOGIN_AUTHENTICATION,
        }
        return make_conninfo(**parameters)


@pytest.fixture
def role_dsn(pg_conn: psycopg.Connection) -> Iterator[RoleDsns]:
    """Create one uniquely named LOGIN principal for each non-login runtime role."""
    migrate.apply_migrations(pg_conn)
    database_suffix = pg_conn.info.dbname[-16:].replace("-", "_")
    logins = {
        "kdive_server": f"kdive_server_{database_suffix}",
        "kdive_worker": f"kdive_worker_{database_suffix}",
        "kdive_reconciler": f"kdive_reconciler_{database_suffix}",
        "kdive_lifecycle_witness": f"kdive_witness_{database_suffix}",
        "unprivileged": f"kdive_unprivileged_{database_suffix}",
    }
    for login in logins.values():
        pg_conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(login)))
    for role, login in logins.items():
        membership = (
            SQL("") if role == "unprivileged" else SQL(" IN ROLE {}").format(Identifier(role))
        )
        pg_conn.execute(
            SQL("CREATE ROLE {} LOGIN PASSWORD {}{}").format(
                Identifier(login), Literal(_LOGIN_AUTHENTICATION), membership
            )
        )

    yield RoleDsns(dict(pg_conn.info.get_parameters()), logins)

    for login in logins.values():
        pg_conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(login)))


@pytest.fixture
def residual_privilege_role_dsn(pg_conn: psycopg.Connection) -> Iterator[RoleDsns]:
    """Apply the authority migrations over compatible roles carrying residual grants."""
    suffix = pg_conn.info.dbname[-10:].replace("-", "_")
    roles = {
        "kdive_server": f"kdive_acl_server_{suffix}",
        "kdive_worker": f"kdive_acl_worker_{suffix}",
        "kdive_reconciler": f"kdive_acl_reconciler_{suffix}",
        "kdive_lifecycle_witness": f"kdive_acl_witness_{suffix}",
    }
    logins = {role: f"{isolated}_login" for role, isolated in roles.items()}
    role_list = SQL(", ").join(Identifier(role) for role in roles.values())
    for role in roles.values():
        pg_conn.execute(
            SQL(
                "CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ).format(Identifier(role))
        )
    for canonical, login in logins.items():
        pg_conn.execute(
            SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                Identifier(login),
                Literal(_LOGIN_AUTHENTICATION),
                Identifier(roles[canonical]),
            )
        )

    try:
        pg_conn.execute(SQL("ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO {}").format(role_list))
        pg_conn.execute(
            SQL("ALTER DEFAULT PRIVILEGES GRANT EXECUTE ON FUNCTIONS TO {}").format(role_list)
        )
        for migration in migrate.discover_migrations():
            pg_conn.execute(migration.sql.encode())
            if migration.version == "0103":
                break
        pg_conn.execute(
            SQL(
                "GRANT ALL ON TABLE worker_incarnations, investigation_build_uses, "
                "investigation_build_use_recoveries TO {}"
            ).format(role_list)
        )
        for filename in (
            "0104_worker_fence_roles.sql",
            "0105_worker_fence_functions.sql",
            "0106_worker_fence_protocol_claim.sql",
        ):
            role_sql = (migrate.SCHEMA_DIR / filename).read_bytes()
            for canonical, isolated in roles.items():
                role_sql = role_sql.replace(canonical.encode(), isolated.encode())
            pg_conn.execute(role_sql)
        yield RoleDsns(dict(pg_conn.info.get_parameters()), logins)
    finally:
        pg_conn.execute(
            SQL("ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES FROM {}").format(role_list)
        )
        pg_conn.execute(
            SQL("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM {}").format(role_list)
        )
        for login in logins.values():
            pg_conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(login)))
        for role in roles.values():
            pg_conn.execute(SQL("DROP OWNED BY {}").format(Identifier(role)))
            pg_conn.execute(SQL("DROP ROLE {}").format(Identifier(role)))


def _login_operation_succeeds(conn: psycopg.Connection, operation: str) -> bool:
    try:
        if operation == "direct_terminate":
            conn.execute("UPDATE worker_incarnations SET state = 'terminated'")
        elif operation == "register":
            conn.execute(
                "SELECT public.register_worker_incarnation(%s, %s, %s, %s, %s)",
                ("docker:authority-test", "docker", Jsonb({}), bytes(32), 1),
            )
        elif operation == "terminate_function":
            conn.execute(
                "SELECT public.terminate_worker_incarnation(%s, %s)", ("missing", "failed")
            )
        elif operation == "direct_delete_use":
            conn.execute("DELETE FROM investigation_build_uses")
        else:  # pragma: no cover - parameterization owns the operation names.
            raise AssertionError(f"unknown operation {operation}")
    except psycopg.Error:
        conn.rollback()
        return False
    return True


def _register(
    role_dsn: RoleDsns,
    incarnation: str,
    credential_hash: bytes,
    *,
    binding: dict[str, object] | None = None,
) -> None:
    with psycopg.connect(role_dsn("kdive_lifecycle_witness"), autocommit=True) as conn:
        conn.execute(
            "SELECT public.register_worker_incarnation(%s, %s, %s, %s, %s)",
            (
                incarnation,
                "docker",
                Jsonb(binding or {"container_id": "a" * 64}),
                credential_hash,
                CURRENT_WORKER_FENCE_PROTOCOL,
            ),
        )


def _seed_claim(
    conn: psycopg.Connection,
    *,
    holder: str,
    project: str = "project-a",
    attempt: int = 1,
) -> tuple[UUID, UUID, UUID]:
    investigation_id, generation, run_id, job_id = uuid4(), uuid4(), uuid4(), uuid4()
    digest = "d" * 64
    build_ref = f"{digest}.{generation}"
    conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'principal', %s, 'title', 'active')",
        (investigation_id, project),
    )
    conn.execute(
        "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
        "content_digest, canonical_document, build_result, artifacts, target_kind, "
        "build_profile, expires_at) VALUES "
        "(%s, %s, %s, %s, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, "
        "'local-libvirt', '{}'::jsonb, now() + interval '1 day')",
        (investigation_id, generation, build_ref, digest),
    )
    conn.execute(
        "INSERT INTO runs (id, investigation_id, state, build_profile, target_kind, "
        "principal, project, build_ref) VALUES "
        "(%s, %s, 'running', '{}'::jsonb, 'local-libvirt', 'principal', %s, %s)",
        (run_id, investigation_id, project, build_ref),
    )
    conn.execute(
        "INSERT INTO jobs (id, kind, state, attempt, max_attempts, worker_id, "
        "lease_expires_at, payload, authorizing, dedup_key) VALUES "
        "(%s, 'install', 'running', %s, 3, %s, now() + interval '5 minutes', "
        "%s, %s, %s)",
        (
            job_id,
            attempt,
            holder,
            Jsonb({"run_id": str(run_id)}),
            Jsonb({"principal": "principal", "project": project}),
            f"worker-fence-{job_id}",
        ),
    )
    return investigation_id, generation, job_id


def _seed_queued_job(conn: psycopg.Connection) -> UUID:
    job_id = uuid4()
    conn.execute(
        "INSERT INTO jobs (id, kind, state, max_attempts, payload, authorizing, dedup_key) "
        "VALUES (%s, 'install', 'queued', 3, '{}'::jsonb, "
        '\'{"principal":"principal","project":"project-a"}\'::jsonb, %s)',
        (job_id, f"worker-fence-lease-{job_id}"),
    )
    return job_id


def _acquire(
    role_dsn: RoleDsns,
    use_id: UUID,
    investigation_id: UUID,
    generation: UUID,
    job_id: UUID,
    attempt: int,
    credential_hash: bytes,
) -> bool:
    with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as conn:
        row = conn.execute(
            "SELECT public.acquire_investigation_build_use(%s, %s, %s, %s, %s, %s)",
            (use_id, investigation_id, generation, job_id, attempt, credential_hash),
        ).fetchone()
    assert row is not None
    return bool(row[0])


@pytest.mark.parametrize(
    ("role", "operation", "allowed"),
    [
        ("kdive_worker", "direct_terminate", False),
        ("kdive_lifecycle_witness", "register", True),
        ("kdive_worker", "terminate_function", False),
        ("kdive_reconciler", "direct_delete_use", False),
        ("unprivileged", "register", False),
    ],
)
def test_worker_fence_role_matrix(
    role: str, operation: str, allowed: bool, role_dsn: RoleDsns
) -> None:
    """Only the witness can record a worker incarnation through its bounded API."""
    with psycopg.connect(role_dsn(role), autocommit=True) as role_conn:
        assert _login_operation_succeeds(role_conn, operation) is allowed


def test_worker_fence_roles_and_login_memberships_are_exact(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Runtime capabilities cannot log in or inherit escalation roles."""
    runtime_roles = {
        "kdive_server",
        "kdive_worker",
        "kdive_reconciler",
        "kdive_lifecycle_witness",
    }
    rows = pg_conn.execute(
        "SELECT rolname, rolcanlogin, rolinherit, rolsuper, rolcreaterole, rolcreatedb, "
        "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
        (list(runtime_roles),),
    ).fetchall()
    assert {row[0] for row in rows} == runtime_roles
    assert all(row[1:] == (False, False, False, False, False, False, False) for row in rows)

    escalation_memberships = pg_conn.execute(
        "SELECT member_role.rolname, granted_role.rolname FROM pg_auth_members m "
        "JOIN pg_roles member_role ON member_role.oid = m.member "
        "JOIN pg_roles granted_role ON granted_role.oid = m.roleid "
        "WHERE member_role.rolname = ANY(%s)",
        (list(runtime_roles),),
    ).fetchall()
    assert escalation_memberships == []

    login_memberships = pg_conn.execute(
        "SELECT member_role.rolname, array_agg(granted_role.rolname ORDER BY granted_role.rolname) "
        "FROM pg_roles member_role "
        "LEFT JOIN pg_auth_members m ON m.member = member_role.oid "
        "LEFT JOIN pg_roles granted_role ON granted_role.oid = m.roleid "
        "WHERE member_role.rolname = ANY(%s) GROUP BY member_role.rolname",
        (list(role_dsn.logins.values()),),
    ).fetchall()
    expected = {
        login: ([role] if role != "unprivileged" else [None])
        for role, login in role_dsn.logins.items()
    }
    assert dict(login_memberships) == expected


def test_migration_upgrade_scrubs_residual_table_mutation_grants(
    residual_privilege_role_dsn: RoleDsns,
) -> None:
    """Compatible runtime roles lose explicit and default protected-table mutation grants."""
    tables = [
        "worker_incarnations",
        "investigation_build_uses",
        "investigation_build_use_recoveries",
    ]
    for role in residual_privilege_role_dsn.logins:
        with psycopg.connect(residual_privilege_role_dsn(role), autocommit=True) as runtime:
            for table in tables:
                for privilege in ("INSERT", "UPDATE", "DELETE"):
                    effective = runtime.execute(
                        "SELECT has_table_privilege(current_user, %s, %s)",
                        (table, privilege),
                    ).fetchone()
                    assert effective == (False,)

    denied_mutations: dict[str, LiteralString] = {
        "kdive_server": "DELETE FROM worker_incarnations",
        "kdive_worker": "DELETE FROM investigation_build_uses",
        "kdive_reconciler": "DELETE FROM investigation_build_use_recoveries",
        "kdive_lifecycle_witness": "UPDATE worker_incarnations SET state = 'terminated'",
    }
    for role, operation in denied_mutations.items():
        with (
            psycopg.connect(residual_privilege_role_dsn(role), autocommit=True) as runtime,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            runtime.execute(SQL(operation))


def test_runtime_roles_receive_data_access_without_crossing_fence_authority(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Runtime processes can use ordinary data while guarded evidence stays API-only."""
    ordinary_tables = {
        str(row[0])
        for row in pg_conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        ).fetchall()
    } - _PROTECTED_TABLES
    sequences = {
        str(row[0])
        for row in pg_conn.execute(
            "SELECT sequence_name FROM information_schema.sequences "
            "WHERE sequence_schema = 'public'"
        ).fetchall()
    }
    assert ordinary_tables == _ORDINARY_TABLES

    for role, login in role_dsn.logins.items():
        expected_by_operation = _EXPECTED_ROLE_TABLE_PRIVILEGES[role]
        for table in ordinary_tables | _PROTECTED_TABLES:
            for privilege in (
                "DELETE",
                "INSERT",
                "REFERENCES",
                "SELECT",
                "TRIGGER",
                "TRUNCATE",
                "UPDATE",
            ):
                effective = pg_conn.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (login, f"public.{table}", privilege),
                ).fetchone()
                assert effective == (table in expected_by_operation.get(privilege, set()),), (
                    role,
                    table,
                    privilege,
                )
        for sequence in sequences:
            privileges = pg_conn.execute(
                "SELECT has_sequence_privilege(%s, %s, privilege) "
                "FROM unnest(%s::text[]) AS privilege ORDER BY privilege",
                (login, f"public.{sequence}", ["SELECT", "UPDATE", "USAGE"]),
            ).fetchall()
            assert privileges == [(False,)] * 3, (role, sequence)


@pytest.mark.parametrize(
    "operation",
    [
        "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
        "VALUES ('install', '{}'::jsonb, 'queued', 3, '{}'::jsonb, 'worker-bypass')",
        "UPDATE jobs SET state = 'running', worker_id = 'observed-current-worker'",
        "UPDATE jobs SET state = 'failed' WHERE state = 'running'",
        "DELETE FROM jobs",
    ],
)
def test_worker_role_cannot_mutate_jobs_directly(
    role_dsn: RoleDsns, operation: LiteralString
) -> None:
    """A worker login cannot bypass credential-gated job transitions with table DML."""
    with (
        psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        worker.execute(SQL(operation))


def test_worker_job_functions_fence_credential_holder_and_attempt(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Guarded job writes derive the holder and match the exact charged attempt."""
    holder_a, holder_b = "docker:job-owner-a", "docker:job-owner-b"
    credential_a, credential_b = b"a" * 32, b"b" * 32
    _register(role_dsn, holder_a, credential_a)
    _register(role_dsn, holder_b, credential_b)

    _investigation, _generation, heartbeat_job = _seed_claim(pg_conn, holder=holder_a, attempt=1)
    _investigation, _generation, complete_job = _seed_claim(pg_conn, holder=holder_a, attempt=2)
    _investigation, _generation, requeue_job = _seed_claim(pg_conn, holder=holder_a, attempt=1)
    _investigation, _generation, fail_job = _seed_claim(pg_conn, holder=holder_a, attempt=3)

    with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker:
        assert worker.execute(
            "SELECT public.heartbeat_worker_job(%s, %s, 1, interval '5 minutes')",
            (heartbeat_job, credential_a),
        ).fetchone() == (True,)
        assert worker.execute(
            "SELECT public.heartbeat_worker_job(%s, %s, 2, interval '5 minutes')",
            (heartbeat_job, credential_a),
        ).fetchone() == (False,)
        assert (
            worker.execute(
                "SELECT public.complete_worker_job(%s, %s, 2, 'result-a')",
                (complete_job, credential_b),
            ).fetchone()
            is None
        )
        completed = worker.execute(
            "SELECT state, result_ref FROM public.complete_worker_job(%s, %s, 2, 'result-a')",
            (complete_job, credential_a),
        ).fetchone()
        assert completed == ("succeeded", "result-a")
        requeued = worker.execute(
            "SELECT state, worker_id FROM public.fail_worker_job("
            "%s, %s, 1, 'infrastructure_failure', '{}'::jsonb, false)",
            (requeue_job, credential_a),
        ).fetchone()
        assert requeued == ("queued", None)
        failed = worker.execute(
            "SELECT state, error_category FROM public.fail_worker_job("
            "%s, %s, 3, 'build_failure', '{\"failure_message\":\"bounded\"}'::jsonb, false)",
            (fail_job, credential_a),
        ).fetchone()
        assert failed == ("failed", "build_failure")


@pytest.mark.parametrize(
    "lease",
    [
        "0 seconds",
        "-1 microsecond",
        "1 hour 1 microsecond",
        pytest.param("1000000 years", id="timestamp-overflow"),
        pytest.param("-1 year 360 days 30 minutes", id="calendar-expired"),
        pytest.param("1 year -360 days 30 minutes", id="calendar-over-limit"),
    ],
)
def test_worker_claim_rejects_out_of_contract_lease_without_state_change(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns, lease: str
) -> None:
    """Hostile worker SQL cannot claim with an expired or over-limit lease."""
    holder, credential = "docker:bounded-claim", b"c" * 32
    _register(role_dsn, holder, credential)
    job_id = _seed_queued_job(pg_conn)

    with (
        psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker,
        pytest.raises(psycopg.errors.InvalidParameterValue, match="lease"),
    ):
        worker.execute(
            "SELECT * FROM public.claim_worker_job(%s, %s, %s::interval, %s::text[])",
            (holder, credential, lease, ["default"]),
        )

    assert pg_conn.execute(
        "SELECT state, attempt, worker_id, lease_expires_at, heartbeat_at FROM jobs WHERE id = %s",
        (job_id,),
    ).fetchone() == ("queued", 0, None, None, None)


@pytest.mark.parametrize(
    "lease",
    [
        "0 seconds",
        "-1 microsecond",
        "1 hour 1 microsecond",
        pytest.param("1000000 years", id="timestamp-overflow"),
        pytest.param("-1 year 360 days 30 minutes", id="calendar-expired"),
        pytest.param("1 year -360 days 30 minutes", id="calendar-over-limit"),
    ],
)
def test_worker_heartbeat_rejects_out_of_contract_lease_without_state_change(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns, lease: str
) -> None:
    """Hostile worker SQL cannot expire or overextend an owned running attempt."""
    holder, credential = "docker:bounded-heartbeat", b"h" * 32
    _register(role_dsn, holder, credential)
    _investigation, _generation, job_id = _seed_claim(pg_conn, holder=holder, attempt=1)
    before = pg_conn.execute(
        "SELECT state, attempt, worker_id, lease_expires_at, heartbeat_at FROM jobs WHERE id = %s",
        (job_id,),
    ).fetchone()

    with (
        psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker,
        pytest.raises(psycopg.errors.InvalidParameterValue, match="lease"),
    ):
        worker.execute(
            "SELECT public.heartbeat_worker_job(%s, %s, 1, %s::interval)",
            (job_id, credential, lease),
        )

    assert (
        pg_conn.execute(
            "SELECT state, attempt, worker_id, lease_expires_at, heartbeat_at "
            "FROM jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
        == before
    )


def test_worker_claim_and_heartbeat_accept_one_hour_lease_ceiling(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """The one-hour lease ceiling is inclusive for current credential-bound paths."""
    holder, credential = "docker:max-valid-lease", b"m" * 32
    _register(role_dsn, holder, credential)
    job_id = _seed_queued_job(pg_conn)

    with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker:
        claimed = worker.execute(
            "SELECT state, attempt FROM public.claim_worker_job("
            "%s, %s, interval '1 hour', %s::text[])",
            (holder, credential, ["default"]),
        ).fetchone()
        assert claimed == ("running", 1)
        assert worker.execute(
            "SELECT public.heartbeat_worker_job(%s, %s, 1, interval '1 hour')",
            (job_id, credential),
        ).fetchone() == (True,)


def test_worker_lease_uses_postgres_clock_at_each_function_invocation(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """A caller's older transaction timestamp cannot backdate claim or heartbeat leases."""
    holder, credential = "docker:invocation-clock", b"t" * 32
    _register(role_dsn, holder, credential)
    job_id = _seed_queued_job(pg_conn)

    with psycopg.connect(role_dsn("kdive_worker")) as worker:
        worker.execute("SELECT 1")  # establish a transaction timestamp before the boundary call
        claim_reference = worker.execute("SELECT clock_timestamp()").fetchone()
        assert claim_reference is not None
        claimed = worker.execute(
            "SELECT heartbeat_at FROM public.claim_worker_job("
            "%s, %s, interval '5 minutes', %s::text[])",
            (holder, credential, ["default"]),
        ).fetchone()
        assert claimed is not None
        assert claimed[0] >= claim_reference[0]

        heartbeat_reference = worker.execute("SELECT clock_timestamp()").fetchone()
        assert heartbeat_reference is not None
        assert worker.execute(
            "SELECT public.heartbeat_worker_job(%s, %s, 1, interval '5 minutes')",
            (job_id, credential),
        ).fetchone() == (True,)
        heartbeat = worker.execute(
            "SELECT heartbeat_at FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()
        assert heartbeat is not None
        assert heartbeat[0] >= heartbeat_reference[0]


def test_worker_job_function_execute_authority_is_exact(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Only the worker process role can invoke credential-bound job writes."""
    signatures = {
        "heartbeat_worker_job(uuid,bytea,integer,interval)",
        "complete_worker_job(uuid,bytea,integer,text)",
        "fail_worker_job(uuid,bytea,integer,text,jsonb,boolean)",
    }
    for signature in signatures:
        for role, login in role_dsn.logins.items():
            privilege = pg_conn.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                (login, signature),
            ).fetchone()
            assert privilege == (role == "kdive_worker",), (role, signature)


def test_migration_upgrade_resets_guarded_function_matrix(
    pg_conn: psycopg.Connection, residual_privilege_role_dsn: RoleDsns
) -> None:
    """Default EXECUTE residue is removed before the intended guarded grants are restored."""
    allowed = {
        "register_worker_incarnation(text,text,jsonb,bytea,integer)": {"kdive_lifecycle_witness"},
        "authenticate_worker_incarnation(bytea)": {"kdive_worker"},
        "terminate_worker_incarnation(text,text)": {"kdive_lifecycle_witness"},
        "acquire_investigation_build_use(uuid,uuid,uuid,uuid,integer,bytea)": {"kdive_worker"},
        "release_investigation_build_use(uuid,bytea)": {"kdive_worker"},
        "recover_investigation_build_use(uuid,text,text,text)": {"kdive_reconciler"},
        "claim_worker_job(text,bytea,interval,text[])": {"kdive_worker"},
    }
    for signature, allowed_roles in allowed.items():
        for canonical, login in residual_privilege_role_dsn.logins.items():
            privilege = pg_conn.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                (login, signature),
            ).fetchone()
            assert privilege == (canonical in allowed_roles,)

    credential = b"u" * 32
    with psycopg.connect(
        residual_privilege_role_dsn("kdive_lifecycle_witness"), autocommit=True
    ) as witness:
        witness.execute(
            "SELECT public.register_worker_incarnation(%s, 'docker', '{}'::jsonb, %s, 1)",
            ("docker:upgrade-authority", credential),
        )
    with psycopg.connect(residual_privilege_role_dsn("kdive_worker"), autocommit=True) as worker:
        authenticated = worker.execute(
            "SELECT incarnation FROM public.authenticate_worker_incarnation(%s)", (credential,)
        ).fetchone()
    assert authenticated == ("docker:upgrade-authority",)
    with psycopg.connect(
        residual_privilege_role_dsn("kdive_reconciler"), autocommit=True
    ) as reconciler:
        recovered = reconciler.execute(
            "SELECT public.recover_investigation_build_use(%s, 'project', 'actor', 'reason')",
            (uuid4(),),
        ).fetchone()
    assert recovered == (False,)


@pytest.mark.parametrize(
    "collision_kind",
    ["login", "membership"],
)
def test_preexisting_runtime_role_collision_fails_closed(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns, collision_kind: str
) -> None:
    """Migration refuses a matching global name with login or escalation capability."""
    assert role_dsn("kdive_worker")
    role_sql = (migrate.SCHEMA_DIR / "0104_worker_fence_roles.sql").read_bytes()
    escalation_role = f"kdive_escalation_{pg_conn.info.dbname[-12:]}"
    pg_conn.execute("BEGIN")
    try:
        if collision_kind == "login":
            pg_conn.execute("ALTER ROLE kdive_worker LOGIN")
        else:
            pg_conn.execute(SQL("CREATE ROLE {} NOLOGIN").format(Identifier(escalation_role)))
            pg_conn.execute(SQL("GRANT {} TO kdive_worker").format(Identifier(escalation_role)))
        with pytest.raises(psycopg.errors.RaiseException, match="incompatible attributes"):
            pg_conn.execute(role_sql)
    finally:
        pg_conn.execute("ROLLBACK")


def test_concurrent_exact_runtime_role_creation_is_idempotent(
    pg_conn: psycopg.Connection, postgres_url: str
) -> None:
    """A concurrent exact role winner is revalidated instead of failing migration."""
    migrate.apply_migrations(pg_conn)
    suffix = pg_conn.info.dbname[-10:].replace("-", "_")
    roles = {
        "kdive_server": f"kdive_race_server_{suffix}",
        "kdive_worker": f"kdive_race_worker_{suffix}",
        "kdive_reconciler": f"kdive_race_reconciler_{suffix}",
        "kdive_lifecycle_witness": f"kdive_race_witness_{suffix}",
    }
    role_sql = (migrate.SCHEMA_DIR / "0104_worker_fence_roles.sql").read_bytes()
    for canonical, isolated in roles.items():
        role_sql = role_sql.replace(canonical.encode(), isolated.encode())

    creator = psycopg.connect(postgres_url)
    contender = psycopg.connect(postgres_url, autocommit=True)
    first_role = roles["kdive_server"]
    started = Event()

    def apply_role_migration() -> None:
        started.set()
        contender.execute(role_sql)

    try:
        creator.execute(
            SQL(
                "CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ).format(Identifier(first_role))
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(apply_role_migration)
            assert started.wait(timeout=2)
            deadline = monotonic() + 2
            while monotonic() < deadline:
                blocked = pg_conn.execute(
                    "SELECT %s = ANY(pg_blocking_pids(%s))",
                    (creator.info.backend_pid, contender.info.backend_pid),
                ).fetchone()
                if blocked == (True,):
                    break
                sleep(0.01)
            else:
                pytest.fail("role migration did not block on concurrent exact role creation")
            with pytest.raises(TimeoutError):
                future.result(timeout=0.5)
            creator.commit()
            future.result(timeout=2)
    finally:
        creator.rollback()
        contender.close()
        creator.close()
        for role in roles.values():
            if pg_conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                pg_conn.execute(SQL("DROP OWNED BY {}").format(Identifier(role)))
                pg_conn.execute(SQL("DROP ROLE {}").format(Identifier(role)))


def test_runtime_role_grant_closes_validation_to_drop_window(
    pg_conn: psycopg.Connection, postgres_url: str
) -> None:
    """A validated role owns its schema capability before another session can drop it."""
    migrate.apply_migrations(pg_conn)
    suffix = pg_conn.info.dbname[-10:].replace("-", "_")
    roles = {
        "kdive_server": f"kdive_drop_server_{suffix}",
        "kdive_worker": f"kdive_drop_worker_{suffix}",
        "kdive_reconciler": f"kdive_drop_reconciler_{suffix}",
        "kdive_lifecycle_witness": f"kdive_drop_witness_{suffix}",
    }
    role_sql = (migrate.SCHEMA_DIR / "0104_worker_fence_roles.sql").read_bytes()
    for canonical, isolated in roles.items():
        role_sql = role_sql.replace(canonical.encode(), isolated.encode())
    pause_key = uuid4().int % (2**63 - 1)
    role_sql = role_sql.replace(
        b"$$;\n\nREVOKE",
        f"$$;\n\nSELECT pg_advisory_xact_lock({pause_key});\n\nREVOKE".encode(),
        1,
    )

    for role in roles.values():
        pg_conn.execute(
            SQL(
                "CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ).format(Identifier(role))
        )

    blocker = psycopg.connect(postgres_url)
    contender = psycopg.connect(postgres_url, autocommit=True)
    dropper = psycopg.connect(postgres_url, autocommit=True)
    started = Event()

    def apply_role_migration() -> None:
        started.set()
        contender.execute(role_sql)

    try:
        blocker.execute("SELECT pg_advisory_xact_lock(%s)", (pause_key,))
        with ThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(apply_role_migration)
            assert started.wait(timeout=2)
            deadline = monotonic() + 2
            while monotonic() < deadline:
                blocked = pg_conn.execute(
                    "SELECT %s = ANY(pg_blocking_pids(%s))",
                    (blocker.info.backend_pid, contender.info.backend_pid),
                ).fetchone()
                if blocked == (True,):
                    break
                sleep(0.01)
            else:
                pytest.fail("role migration did not reach the post-validation pause")
            with pytest.raises(TimeoutError):
                future.result(timeout=0.5)
            drop_future = executor.submit(
                dropper.execute,
                SQL("DROP ROLE {}").format(Identifier(roles["kdive_server"])),
            )
            drop_was_blocked = True
            try:
                drop_future.result(timeout=0.5)
                drop_was_blocked = False
            except TimeoutError:
                pass
            finally:
                blocker.commit()
            if drop_was_blocked:
                future.result(timeout=2)
                with pytest.raises(psycopg.errors.DependentObjectsStillExist):
                    drop_future.result(timeout=2)
            assert drop_was_blocked
    finally:
        blocker.rollback()
        dropper.close()
        contender.close()
        blocker.close()
        for role in roles.values():
            if pg_conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                pg_conn.execute(SQL("DROP OWNED BY {}").format(Identifier(role)))
                pg_conn.execute(SQL("DROP ROLE {}").format(Identifier(role)))


def test_acquire_derives_exact_locked_job_claim(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """A supplied job and attempt are only accepted when the credential owns that claim."""
    holder_a, holder_b = "docker:holder-a", "docker:holder-b"
    credential_a = b"a" * 32
    _register(role_dsn, holder_a, credential_a)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder_b, attempt=2)

    use_id = uuid4()
    assert not _acquire(
        role_dsn,
        use_id,
        investigation_id,
        generation,
        job_id,
        2,
        credential_a,
    )
    assert pg_conn.execute(
        "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s", (use_id,)
    ).fetchone() == (0,)


def test_acquire_persists_attempt_and_lease_from_locked_claim(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """The durable use copies claim facts from the locked jobs row."""
    holder, credential = "docker:exact-holder", b"e" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder, attempt=3)
    lease = pg_conn.execute("SELECT lease_expires_at FROM jobs WHERE id = %s", (job_id,)).fetchone()
    assert lease is not None

    use_id = uuid4()
    assert _acquire(
        role_dsn,
        use_id,
        investigation_id,
        generation,
        job_id,
        3,
        credential,
    )
    row = pg_conn.execute(
        "SELECT job_id, attempt, holder_worker_id, lease_expires_at "
        "FROM investigation_build_uses WHERE use_id = %s",
        (use_id,),
    ).fetchone()
    assert row == (job_id, 3, holder, lease[0])


def test_acquire_refuses_generation_outside_claimed_run(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """A valid worker claim cannot pin a different Run's generation."""
    holder, credential = "docker:wrong-generation", b"g" * 32
    _register(role_dsn, holder, credential)
    _claimed_investigation, _claimed_generation, job_id = _seed_claim(pg_conn, holder=holder)
    other_investigation, other_generation, _other_job = _seed_claim(pg_conn, holder=holder)

    assert not _acquire(
        role_dsn,
        uuid4(),
        other_investigation,
        other_generation,
        job_id,
        1,
        credential,
    )


def test_acquire_refuses_claim_with_mismatched_authorizing_project(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """The job, Run, and Investigation must retain one authoritative project."""
    holder, credential = "docker:wrong-project", b"p" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder, project="project-a")
    pg_conn.execute(
        "UPDATE jobs SET authorizing = %s WHERE id = %s",
        (Jsonb({"principal": "principal", "project": "project-b"}), job_id),
    )

    assert not _acquire(
        role_dsn,
        uuid4(),
        investigation_id,
        generation,
        job_id,
        1,
        credential,
    )


def test_reclaiming_generation_serializes_before_acquisition(
    pg_conn: psycopg.Connection, postgres_url: str, role_dsn: RoleDsns
) -> None:
    """Acquisition waits for the generation lock and then refuses reclaiming state."""
    holder, credential = "docker:reclaim-race", b"q" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder)
    use_id = uuid4()
    connected = Event()
    worker_pid: list[int] = []

    def acquire() -> bool:
        with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker:
            worker_pid.append(worker.info.backend_pid)
            connected.set()
            row = worker.execute(
                "SELECT public.acquire_investigation_build_use(%s, %s, %s, %s, %s, %s)",
                (use_id, investigation_id, generation, job_id, 1, credential),
            ).fetchone()
        assert row is not None
        return bool(row[0])

    with (
        psycopg.connect(postgres_url) as reclaim,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        reclaim.execute(
            "UPDATE investigation_builds SET state = 'reclaiming' "
            "WHERE investigation_id = %s AND generation = %s",
            (investigation_id, generation),
        )
        future = executor.submit(acquire)
        assert connected.wait(timeout=2)
        deadline = monotonic() + 2
        while monotonic() < deadline:
            blocked = pg_conn.execute(
                "SELECT %s = ANY(pg_blocking_pids(%s))",
                (reclaim.info.backend_pid, worker_pid[0]),
            ).fetchone()
            if blocked == (True,):
                break
            sleep(0.01)
        else:
            pytest.fail("acquisition did not block on the reclaiming generation row")
        with pytest.raises(TimeoutError):
            future.result(timeout=0.5)
        reclaim.commit()
        assert future.result(timeout=2) is False

    assert pg_conn.execute(
        "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s", (use_id,)
    ).fetchone() == (0,)


def test_acquire_refuses_replaced_attempt(pg_conn: psycopg.Connection, role_dsn: RoleDsns) -> None:
    """A stale provider cannot pin bytes after its job claim has been replaced."""
    holder, replacement = "docker:stale", "docker:replacement"
    credential = b"s" * 32
    _register(role_dsn, holder, credential)
    _register(role_dsn, replacement, b"t" * 32)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder, attempt=1)
    pg_conn.execute(
        "UPDATE jobs SET worker_id = %s, attempt = 2 WHERE id = %s", (replacement, job_id)
    )

    assert not _acquire(
        role_dsn,
        uuid4(),
        investigation_id,
        generation,
        job_id,
        1,
        credential,
    )


def test_release_refuses_replaced_attempt(pg_conn: psycopg.Connection, role_dsn: RoleDsns) -> None:
    """A credential cannot release a use after the job advances to another attempt."""
    holder, credential = "docker:release-holder", b"r" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder, attempt=1)
    use_id = uuid4()
    pg_conn.execute(
        "INSERT INTO investigation_build_uses (use_id, investigation_id, generation, job_id, "
        "attempt, holder_worker_id, lease_expires_at) VALUES "
        "(%s, %s, %s, %s, 1, %s, now() + interval '5 minutes')",
        (use_id, investigation_id, generation, job_id, holder),
    )
    pg_conn.execute("UPDATE jobs SET attempt = 2 WHERE id = %s", (job_id,))

    with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker:
        released = worker.execute(
            "SELECT public.release_investigation_build_use(%s, %s)", (use_id, credential)
        ).fetchone()
    assert released == (False,)
    assert pg_conn.execute(
        "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s", (use_id,)
    ).fetchone() == (1,)


def test_termination_serializes_before_acquisition(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Acquisition blocks on termination's incarnation lock and then observes terminal state."""
    holder, credential = "docker:termination-race", b"t" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(pg_conn, holder=holder, attempt=1)
    use_id = uuid4()
    started = Event()

    def acquire() -> bool:
        with psycopg.connect(role_dsn("kdive_worker"), autocommit=True) as worker:
            started.set()
            row = worker.execute(
                "SELECT public.acquire_investigation_build_use(%s, %s, %s, %s, %s, %s)",
                (use_id, investigation_id, generation, job_id, 1, credential),
            ).fetchone()
        assert row is not None
        return bool(row[0])

    with (
        psycopg.connect(role_dsn("kdive_lifecycle_witness")) as witness,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        assert witness.execute(
            "SELECT public.terminate_worker_incarnation(%s, 'killed')", (holder,)
        ).fetchone() == (True,)
        future = executor.submit(acquire)
        assert started.wait(timeout=2)
        with pytest.raises(TimeoutError):
            future.result(timeout=0.5)
        witness.commit()
        assert future.result(timeout=2) is False

    assert pg_conn.execute(
        "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s", (use_id,)
    ).fetchone() == (0,)


def test_recovery_joins_and_persists_authoritative_project(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """A use ID cannot cross projects, and the accepted project's audit is permanent."""
    holder, credential = "docker:recover-holder", b"v" * 32
    _register(role_dsn, holder, credential)
    investigation_id, generation, job_id = _seed_claim(
        pg_conn, holder=holder, project="project-a", attempt=1
    )
    use_id = uuid4()
    assert _acquire(
        role_dsn,
        use_id,
        investigation_id,
        generation,
        job_id,
        1,
        credential,
    )
    with psycopg.connect(role_dsn("kdive_lifecycle_witness"), autocommit=True) as witness:
        assert witness.execute(
            "SELECT public.terminate_worker_incarnation(%s, 'killed')", (holder,)
        ).fetchone() == (True,)

    with psycopg.connect(role_dsn("kdive_reconciler"), autocommit=True) as reconciler:
        refused = reconciler.execute(
            "SELECT public.recover_investigation_build_use(%s, %s, %s, %s)",
            (use_id, "project-b", "reconciler:test", "worker terminated"),
        ).fetchone()
        recovered = reconciler.execute(
            "SELECT public.recover_investigation_build_use(%s, %s, %s, %s)",
            (use_id, "project-a", "reconciler:test", "worker terminated"),
        ).fetchone()

    assert refused == (False,)
    assert recovered == (True,)
    assert pg_conn.execute(
        "SELECT project, investigation_id, generation FROM investigation_build_use_recoveries "
        "WHERE use_id = %s",
        (use_id,),
    ).fetchone() == ("project-a", investigation_id, generation)


def test_incarnation_identity_bound_is_bytes_at_table_and_function(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """A 512-codepoint multibyte identity cannot exceed the 512-byte persistence cap."""
    oversized_identity = "é" * 512
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
            "fence_protocol, credential_hash) VALUES (%s, 'docker', '{}'::jsonb, 1, %s)",
            (oversized_identity, b"i" * 32),
        )

    with (
        psycopg.connect(role_dsn("kdive_lifecycle_witness"), autocommit=True) as witness,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        witness.execute(
            "SELECT public.register_worker_incarnation(%s, 'docker', '{}'::jsonb, %s, 1)",
            (oversized_identity, b"j" * 32),
        )


def test_authority_binding_bound_is_serialized_bytes_at_table_and_function(
    pg_conn: psycopg.Connection, role_dsn: RoleDsns
) -> None:
    """Both persistence paths reject a JSON object beyond the serialized byte cap."""
    oversized_binding = {"payload": "x" * _BINDING_MAX_BYTES}
    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
            "fence_protocol, credential_hash) VALUES ('docker:large-direct', 'docker', %s, 1, %s)",
            (Jsonb(oversized_binding), b"b" * 32),
        )

    with (
        psycopg.connect(role_dsn("kdive_lifecycle_witness"), autocommit=True) as witness,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        witness.execute(
            "SELECT public.register_worker_incarnation(%s, 'docker', %s, %s, 1)",
            ("docker:large-function", Jsonb(oversized_binding), b"c" * 32),
        )
