"""Real-container proof that shared_container starts exactly one backend and stops it
by id (AC1 real, AC6b). Skips without Docker; hard-fails under KDIVE_REQUIRE_DOCKER."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

from tests.db import conftest as db_conftest
from tests.support import xdist_backend


def _docker_or_skip() -> None:
    if os.environ.get("KDIVE_REQUIRE_DOCKER") == "1":
        return
    try:
        from testcontainers.core.docker_client import DockerClient

        DockerClient().client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Docker unavailable: {exc}")


def test_one_real_container_for_two_holders(tmp_path: Path) -> None:
    _docker_or_skip()
    starts: list[str] = []

    def counting_start() -> tuple[str, str]:
        url, cid = db_conftest._start_postgres()
        starts.append(cid)
        return url, cid

    def acquire():
        return xdist_backend.shared_container(
            tmp_path, "pg-real", start=counting_start, stop=db_conftest._stop_postgres
        )

    with acquire() as url_a, acquire() as url_b:
        assert url_a == url_b
        assert len(starts) == 1  # one real container for two concurrent holders
        max_conn = xdist_backend.postgres_max_connections()
        admin = xdist_backend.with_database_name(url_a, "postgres")
        with psycopg.connect(admin, autocommit=True) as conn:
            row = conn.execute("SHOW max_connections").fetchone()
            assert row is not None
            assert int(row[0]) >= max_conn  # AC6b: server sized for the worker count

    # after the last release the container is gone
    import docker.errors
    from testcontainers.core.docker_client import DockerClient

    with pytest.raises(docker.errors.NotFound):
        DockerClient().client.containers.get(starts[0])
