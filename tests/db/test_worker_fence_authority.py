"""Database authority boundaries for worker-incarnation artifact fences."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from threading import Event
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier, Literal
from psycopg.types.json import Jsonb

from kdive.db import migrate

_LOGIN_AUTHENTICATION = "worker-fence-test-authentication"
_BINDING_MAX_BYTES = 4096


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
                1,
            ),
        )


def _seed_claim(
    conn: psycopg.Connection,
    *,
    holder: str,
    project: str = "project-a",
    attempt: int = 1,
) -> tuple[UUID, UUID, UUID]:
    investigation_id, generation, job_id = uuid4(), uuid4(), uuid4()
    digest = "d" * 64
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
        (investigation_id, generation, f"{digest}.{generation}", digest),
    )
    conn.execute(
        "INSERT INTO jobs (id, kind, state, attempt, max_attempts, worker_id, "
        "lease_expires_at, authorizing, dedup_key) VALUES "
        "(%s, 'install', 'running', %s, 3, %s, now() + interval '5 minutes', "
        "'{}'::jsonb, %s)",
        (job_id, attempt, holder, f"worker-fence-{job_id}"),
    )
    return investigation_id, generation, job_id


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


def test_acquire_refuses_replaced_attempt(pg_conn: psycopg.Connection, role_dsn: RoleDsns) -> None:
    """A stale provider cannot pin bytes after its job claim has been replaced."""
    holder, replacement = "docker:stale", "docker:replacement"
    credential = b"s" * 32
    _register(role_dsn, holder, credential)
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
