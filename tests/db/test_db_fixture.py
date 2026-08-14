"""Cluster-global lifecycle guards in the disposable PostgreSQL fixtures."""

from __future__ import annotations

import psycopg
import pytest
from pytest import MonkeyPatch

from kdive.db import migrate
from tests.db import conftest as db_conftest


def _try_lock(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "SELECT pg_try_advisory_lock(%s, %s)",
        (migrate._LOCK_CLASS_MIGRATION, migrate._LOCK_OBJID),
    ).fetchone()
    assert row is not None
    return bool(row[0])


def test_cluster_global_role_lock_uses_common_maintenance_database(
    postgres_url: str, monkeypatch: MonkeyPatch
) -> None:
    connections: list[psycopg.Connection] = []
    real_connect = psycopg.connect

    def recording_connect(conninfo: str, *, autocommit: bool = False) -> psycopg.Connection:
        conn = real_connect(conninfo, autocommit=autocommit)
        connections.append(conn)
        return conn

    monkeypatch.setattr(db_conftest.psycopg, "connect", recording_connect)
    with db_conftest._cluster_global_role_lock(postgres_url):
        assert connections[-1].info.dbname == "postgres"
        assert not connections[-1].closed
    assert connections[-1].closed


def test_cluster_global_role_lock_timeout_is_actionable(postgres_url: str) -> None:
    admin_url = db_conftest._server_url_without_db(postgres_url)
    with psycopg.connect(admin_url, autocommit=True) as holder:
        holder_pid = holder.info.backend_pid
        holder.execute(
            "SELECT pg_advisory_lock(%s, %s)",
            (migrate._LOCK_CLASS_MIGRATION, migrate._LOCK_OBJID),
        )
        with (
            pytest.raises(RuntimeError) as caught,
            db_conftest._cluster_global_role_lock(postgres_url, timeout_ms=25),
        ):
            pytest.fail("timed-out contender entered the protected operation")

    message = str(caught.value)
    assert "25 milliseconds of PostgreSQL server elapsed time" in message
    assert "per acquisition" in message
    assert "protected operation did not start" in message
    assert "postgres" in message
    assert str(migrate._LOCK_CLASS_MIGRATION) in message
    assert str(migrate._LOCK_OBJID) in message
    assert str(holder_pid) in message
    assert "stop the stuck test worker or let it exit, then rerun" in message


def test_cluster_global_role_lock_releases_when_body_raises(
    postgres_url: str, monkeypatch: MonkeyPatch
) -> None:
    connections: list[psycopg.Connection] = []
    real_connect = psycopg.connect

    def recording_connect(conninfo: str, *, autocommit: bool = False) -> psycopg.Connection:
        conn = real_connect(conninfo, autocommit=autocommit)
        connections.append(conn)
        return conn

    monkeypatch.setattr(db_conftest.psycopg, "connect", recording_connect)
    with (
        pytest.raises(LookupError, match="body failed"),
        db_conftest._cluster_global_role_lock(postgres_url),
    ):
        raise LookupError("body failed")
    assert connections[-1].closed


def test_cluster_global_role_lock_rejects_nonpositive_timeout(postgres_url: str) -> None:
    with (
        pytest.raises(ValueError, match="timeout_ms must be greater than zero"),
        db_conftest._cluster_global_role_lock(postgres_url, timeout_ms=0),
    ):
        pytest.fail("invalid lock acquisition entered the protected operation")


def test_pg_conn_holds_cluster_global_role_lock_for_test_body(
    postgres_url: str, pg_conn: psycopg.Connection
) -> None:
    assert pg_conn.info.dbname
    admin_url = db_conftest._server_url_without_db(postgres_url)
    with psycopg.connect(admin_url, autocommit=True) as contender:
        assert not _try_lock(contender)
