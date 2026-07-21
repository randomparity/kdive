from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from tests.db import conftest as db_conftest
from tests.support import xdist_backend


def _isolate_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point per_run_root at a private dir so these direct _acquire_pg_server calls do
    not read or perturb the real session container's shared coordination state."""
    monkeypatch.setattr(db_conftest.xdist_backend, "per_run_root", lambda _factory: root)


def test_server_url_without_db_strips_path() -> None:
    assert db_conftest._server_url_without_db("postgresql://u:p@h:5432/test") == (
        "postgresql://u:p@h:5432/postgres"
    )


def test_override_is_selected_without_starting_a_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    monkeypatch.setenv("KDIVE_TEST_PG_URL", "postgresql://u:p@h:5432/somedb")

    def _boom() -> tuple[str, str]:
        raise AssertionError("override path must not start a container")

    monkeypatch.setattr(db_conftest, "_start_postgres", _boom)
    with db_conftest._acquire_pg_server(tmp_path_factory, require_docker=False) as server:
        assert server == "postgresql://u:p@h:5432/somedb"


def test_require_docker_reraises_start_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    monkeypatch.delenv("KDIVE_TEST_PG_URL", raising=False)
    _isolate_root(monkeypatch, tmp_path)

    def _boom() -> tuple[str, str]:
        raise RuntimeError("docker down")

    monkeypatch.setattr(db_conftest, "_start_postgres", _boom)
    with (
        pytest.raises(RuntimeError),
        db_conftest._acquire_pg_server(tmp_path_factory, require_docker=True),
    ):
        pass


def test_no_docker_skips_when_not_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    monkeypatch.delenv("KDIVE_TEST_PG_URL", raising=False)
    _isolate_root(monkeypatch, tmp_path)

    def _boom() -> tuple[str, str]:
        raise RuntimeError("docker down")

    monkeypatch.setattr(db_conftest, "_start_postgres", _boom)
    # simulate Docker genuinely down so start-failure becomes a skip (not a real error)
    monkeypatch.setattr(db_conftest.xdist_backend, "docker_available", lambda: False)
    with (
        pytest.raises(pytest.skip.Exception),
        db_conftest._acquire_pg_server(tmp_path_factory, require_docker=False),
    ):
        pass


def test_real_error_propagates_even_when_not_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A start failure while Docker IS up is a real error, not a masked skip."""
    monkeypatch.delenv("KDIVE_TEST_PG_URL", raising=False)
    _isolate_root(monkeypatch, tmp_path)

    def _boom() -> tuple[str, str]:
        raise RuntimeError("disk full writing state")

    monkeypatch.setattr(db_conftest, "_start_postgres", _boom)
    monkeypatch.setattr(db_conftest.xdist_backend, "docker_available", lambda: True)
    with (
        pytest.raises(RuntimeError),
        db_conftest._acquire_pg_server(tmp_path_factory, require_docker=False),
    ):
        pass


def test_provisioning_error_propagates_not_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    monkeypatch.delenv("KDIVE_TEST_PG_URL", raising=False)
    _isolate_root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        db_conftest, "_start_postgres", lambda: ("postgresql://u:p@h:5432/test", "cid")
    )
    monkeypatch.setattr(db_conftest, "_stop_postgres", lambda _cid: None)
    with (
        pytest.raises(ValueError),  # a body error must NOT become pytest.skip
        db_conftest._acquire_pg_server(tmp_path_factory, require_docker=False),
    ):
        raise ValueError("provisioning blew up")


def test_provision_and_drop_roundtrip_against_real_server() -> None:
    xdist_backend.skip_without_docker()
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:17") as container:
        server = container.get_connection_url(driver=None)  # includes the password
        name = db_conftest._worker_db_name()
        worker_url, dbname = db_conftest._provision_worker_db(server, dbname=name)
        assert dbname == name and f"/{name}" in worker_url and name.startswith("kdive_test_")
        with psycopg.connect(worker_url) as conn:  # database exists and is reachable
            conn.execute("CREATE TABLE marker (id int)")  # leftover to detect reclaim
        # AC4: re-provisioning the SAME name reclaims (DROP … FORCE then CREATE), no error
        again_url, _ = db_conftest._provision_worker_db(server, dbname=name)
        assert again_url == worker_url  # same name reused
        with psycopg.connect(again_url) as conn:
            row = conn.execute("SELECT to_regclass('public.marker')").fetchone()
            assert row is not None and row[0] is None  # dropped and recreated clean
        db_conftest._drop_worker_db(server, name)
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(worker_url, connect_timeout=3)
