"""Disposable-Postgres fixtures for the db tests (ADR-0015, ADR-0401, ADR-0419).

`postgres_url` yields a per-worker database on a backend shared for the whole run.
It first honors `KDIVE_TEST_PG_URL` (a running server, e.g. `just compose-up`); with
no override it lazily starts one shared testcontainer coordinated across xdist
workers (`tests/support/xdist_backend`). Each worker owns a
`kdive_test_<worker>_<token>` database; `pg_conn` empties `public` per test — used
only by the migration-runner tests in this directory that deliberately build partial
or staged schema states. Every other db-backed test uses `migrated_url` instead: it
migrates its own per-worker database exactly once (`_migrated_db`, session-scoped)
and resets state per test by truncating every application table and restoring a
snapshot of its post-migration rows (ADR-0419) — far cheaper than replaying all
migrations per test — and is kept on a separate database so `pg_conn`'s
drop+recreate can never invalidate the "already migrated" assumption `migrated_url`
relies on. When Docker is unreachable the fixture skips, unless
`KDIVE_REQUIRE_DOCKER=1`, which re-raises so a broken runner cannot mask the suite.
On a *persistent* override backend, crashed runs leave `kdive_test_*` databases that
must be swept periodically (ADR-0401 residual).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass

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

    testcontainers_config.ryuk_disabled = True  # refcount owns lifecycle (ADR-0401)
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
    with xdist_backend.shared_container_or_skip(
        root, "pg", start=_start_postgres, stop=_stop_postgres, require_docker=require_docker
    ) as server_url:
        yield server_url


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


def _application_tables(conn: psycopg.Connection) -> list[str]:
    """Base tables in ``public``, excluding the migration runner's own bookkeeping."""
    return [
        row[0]
        for row in conn.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
              AND table_name != 'schema_migrations'
            ORDER BY table_name
            """
        ).fetchall()
    ]


def _copy_out(conn: psycopg.Connection, table: str) -> bytes:
    buf = bytearray()
    with (
        conn.cursor() as cur,
        cur.copy(
            sql.SQL("COPY {} TO STDOUT (FORMAT binary)").format(sql.Identifier(table))
        ) as copy,
    ):
        for chunk in copy:
            buf += chunk
    return bytes(buf)


def _copy_in(conn: psycopg.Connection, table: str, data: bytes) -> None:
    with (
        conn.cursor() as cur,
        cur.copy(
            sql.SQL("COPY {} FROM STDIN (FORMAT binary)").format(sql.Identifier(table))
        ) as copy,
    ):
        copy.write(data)


@dataclass(frozen=True)
class _MigratedWorkerDb:
    """A once-per-worker migrated database plus a snapshot of its post-migration rows.

    A handful of migrations seed reference data (`system_shapes`, `build_hosts`, …
    via `INSERT INTO` — see ``src/kdive/db/schema/``); a plain ``TRUNCATE`` would
    permanently discard those rows since migrations never re-run after the once-
    per-worker `apply_migrations` call. Capturing every table's exact bytes right
    after migration and replaying them on every reset restores that seed data too,
    without hard-coding which tables happen to carry it (ADR-0419).
    """

    url: str
    snapshot: dict[str, bytes]


@pytest.fixture(scope="session")
def _migrated_db(postgres_url: str) -> Iterator[_MigratedWorkerDb]:
    """A second per-worker database, migrated exactly once for the session (ADR-0419).

    Deliberately a *different* database than ``postgres_url``: the migration-runner
    tests in this directory use ``pg_conn`` (built on ``postgres_url``) to drop and
    rebuild partial schema states, which would otherwise invalidate the "already
    migrated" assumption ``migrated_url`` relies on for its fast per-test reset.
    Depending on ``postgres_url`` rather than re-running ``_acquire_pg_server``
    costs nothing extra: it derives the same shared server from ``postgres_url``'s
    own conninfo (stripping its database path), so provisioning this second database
    is one more ``CREATE DATABASE`` plus one migration replay per worker, not a
    second container acquisition. It also nests this fixture's teardown inside
    ``postgres_url``'s, so the shared server is still alive when this database is
    dropped.
    """
    server = _server_url_without_db(postgres_url)
    worker_url, dbname = _provision_worker_db(server)
    with psycopg.connect(worker_url, autocommit=True) as conn:
        migrate.apply_migrations(conn)
        snapshot = {table: _copy_out(conn, table) for table in _application_tables(conn)}
    try:
        yield _MigratedWorkerDb(worker_url, snapshot)
    finally:
        _drop_worker_db(server, dbname)


def _reset_to_snapshot(conn: psycopg.Connection, snapshot: dict[str, bytes]) -> None:
    """``TRUNCATE`` every application table, then restore the post-migration snapshot.

    Cheaper than dropping the schema and replaying all migrations: the schema never
    changes between tests, so clearing rows and replaying the one-time seed data is
    enough to isolate one test from the next (ADR-0419).
    """
    if not snapshot:
        return
    names = sql.SQL(", ").join(sql.Identifier(t) for t in snapshot)
    conn.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(names))
    for table, data in snapshot.items():
        _copy_in(conn, table, data)


@pytest.fixture
def migrated_url(_migrated_db: _MigratedWorkerDb) -> str:
    """A migrated database, reset to its post-migration state for this test only.

    Migrations run once per worker (``_migrated_db``); each test starts from that
    exact snapshot via `TRUNCATE` + restore, not a full drop+remigrate. Tests that
    need the real migration runner to build up partial/staged schema states use
    ``pg_conn`` directly instead (see the migration-runner tests in this directory).
    """
    with psycopg.connect(_migrated_db.url, autocommit=True) as conn:
        _reset_to_snapshot(conn, _migrated_db.snapshot)
    return _migrated_db.url
