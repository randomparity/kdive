"""Disposable-Postgres fixtures for the db tests (ADR-0015, ADR-0400).

`postgres_url` yields a per-worker database on a backend shared for the whole run.
It first honors `KDIVE_TEST_PG_URL` (a running server, e.g. `just compose-up`); with
no override it lazily starts one shared testcontainer coordinated across xdist
workers (`tests/support/xdist_backend`). Each worker owns a
`kdive_test_<worker>_<token>` database; `pg_conn` empties `public` per test. When
Docker is unreachable the fixture skips, unless `KDIVE_REQUIRE_DOCKER=1`, which
re-raises so a broken runner cannot mask the suite. On a *persistent* override
backend, crashed runs leave `kdive_test_*` databases that must be swept periodically
(ADR-0400 residual).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress

import psycopg
import pytest
from psycopg import sql

from kdive.db import migrate
from tests.support import xdist_backend

_POSTGRES_IMAGE = "postgres:17"


def _start_postgres() -> tuple[str, str]:
    """Start one shared Postgres testcontainer; return (server_url, container_id)."""
    from testcontainers.core.config import testcontainers_config
    from testcontainers.postgres import PostgresContainer

    testcontainers_config.ryuk_disabled = True  # refcount owns lifecycle (ADR-0400)
    container = PostgresContainer(_POSTGRES_IMAGE).with_command(
        f"postgres -c max_connections={xdist_backend.postgres_max_connections()}"
    )
    container.start()
    return container.get_connection_url(driver=None), container.get_wrapped_container().id


def _stop_postgres(container_id: str) -> None:
    import docker.errors
    from testcontainers.core.docker_client import DockerClient

    with suppress(docker.errors.NotFound):  # already reaped — nothing to stop
        DockerClient().client.containers.get(container_id).remove(force=True)


def _server_url_without_db(url: str) -> str:
    """Strip any database path so we can connect to the server to CREATE DATABASE."""
    return xdist_backend.with_database_name(url, "postgres")


def _worker_db_name() -> str:
    """A fresh per-worker, run-unique database name."""
    return f"kdive_test_{xdist_backend.xdist_worker_id()}_{xdist_backend.worker_namespace_token()}"


def _provision_worker_db(server_url: str, dbname: str | None = None) -> tuple[str, str]:
    """Create a database on `server_url`; return (worker_url, dbname).

    `dbname` defaults to a fresh `_worker_db_name()`; a test may pass an explicit name
    to exercise the same-name `DROP DATABASE IF EXISTS … FORCE` reclaim path.
    """
    dbname = dbname or _worker_db_name()
    ident = sql.Identifier(dbname)
    admin = _server_url_without_db(server_url)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(ident))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(ident))
    return xdist_backend.with_database_name(server_url, dbname), dbname


def _drop_worker_db(server_url: str, dbname: str) -> None:
    admin = _server_url_without_db(server_url)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname))
        )


@contextmanager
def _acquire_pg_server(
    tmp_path_factory: pytest.TempPathFactory, *, require_docker: bool
) -> Iterator[str]:
    """Yield the shared server URL: the override if set, else a shared container.

    Extracted from the fixture so the override / require-docker / skip decisions are
    directly testable (the tests monkeypatch `_start_postgres`).
    """
    override = os.environ.get("KDIVE_TEST_PG_URL")
    if override:
        yield override
        return

    try:
        import testcontainers.postgres  # noqa: F401
    except ImportError as exc:  # pragma: no cover - dev dep always present
        if require_docker:
            raise
        pytest.skip(f"testcontainers not installed: {exc}")

    root = xdist_backend.per_run_root(tmp_path_factory)
    manager = xdist_backend.shared_container(root, "pg", start=_start_postgres, stop=_stop_postgres)
    try:
        server_url = manager.__enter__()  # only container start can fail here
    except Exception as exc:  # Docker daemon unreachable / image pull failure.
        if require_docker:
            raise
        pytest.skip(f"Docker unavailable for testcontainers: {exc}")
    # Yield OUTSIDE the skip-catch: a provisioning/readiness failure in the consumer
    # (CREATE DATABASE rejected, server refusing connections) must surface as a real
    # error, not a misleading "Docker unavailable" skip.
    try:
        yield server_url
    finally:
        manager.__exit__(None, None, None)  # refcount decrement / stop-by-id


@pytest.fixture(scope="session")
def postgres_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    require_docker = os.environ.get("KDIVE_REQUIRE_DOCKER") == "1"
    with _acquire_pg_server(tmp_path_factory, require_docker=require_docker) as server:
        worker_url, dbname = _provision_worker_db(server)
        try:
            yield worker_url
        finally:
            _drop_worker_db(server, dbname)


@pytest.fixture
def pg_conn(postgres_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        yield conn


@pytest.fixture
def migrated_url(pg_conn: psycopg.Connection, postgres_url: str) -> str:
    """A migrated, freshly-emptied database; yields the conninfo for async tests.

    Depends on ``pg_conn`` (which drops and recreates ``public``) so each test starts
    from a clean schema, then applies the migrations on that same autocommit
    connection before handing back the URL for async connections.
    """
    migrate.apply_migrations(pg_conn)
    return postgres_url
