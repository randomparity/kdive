"""Database authority boundaries for worker-incarnation artifact fences."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier, Literal

from kdive.db import migrate

_LOGIN_AUTHENTICATION = "worker-fence-test-authentication"


@pytest.fixture
def role_dsn(pg_conn: psycopg.Connection) -> Iterator[Callable[[str], str]]:
    """Create one LOGIN principal for each non-login runtime role."""
    migrate.apply_migrations(pg_conn)
    logins = {
        "kdive_server": "kdive_test_server_login",
        "kdive_worker": "kdive_test_worker_login",
        "kdive_reconciler": "kdive_test_reconciler_login",
        "kdive_lifecycle_witness": "kdive_test_witness_login",
        "unprivileged": "kdive_test_unprivileged_login",
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

    parameters = dict(pg_conn.info.get_parameters())

    def _for_role(role: str) -> str:
        connection_parameters = {
            **parameters,
            "user": logins[role],
            "password": _LOGIN_AUTHENTICATION,
        }
        return make_conninfo(**connection_parameters)

    yield _for_role

    for login in logins.values():
        pg_conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(login)))


def _login_operation_succeeds(conn: psycopg.Connection, operation: str) -> bool:
    try:
        if operation == "direct_terminate":
            conn.execute("UPDATE worker_incarnations SET state = 'terminated'")
        elif operation == "register":
            conn.execute(
                "SELECT public.register_worker_incarnation(%s, %s, %s::jsonb, %s, %s::integer)",
                ("docker:authority-test", "docker", "{}", bytes(32), 1),
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
    role: str, operation: str, allowed: bool, role_dsn: Callable[[str], str]
) -> None:
    """Only the witness can record a worker incarnation through its bounded API."""
    with psycopg.connect(role_dsn(role), autocommit=True) as role_conn:
        assert _login_operation_succeeds(role_conn, operation) is allowed


def test_worker_fence_roles_are_non_login_and_have_one_runtime_membership(
    pg_conn: psycopg.Connection, role_dsn: Callable[[str], str]
) -> None:
    """Runtime roles are capabilities, while processes authenticate as separate logins."""
    assert role_dsn("kdive_worker")
    rows = pg_conn.execute(
        "SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname LIKE 'kdive_%' ORDER BY rolname"
    ).fetchall()
    assert ("kdive_server", False) in rows
    assert ("kdive_worker", False) in rows
    assert ("kdive_reconciler", False) in rows
    assert ("kdive_lifecycle_witness", False) in rows
