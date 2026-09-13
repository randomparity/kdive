"""Worker-role fixture coverage for handler authorization tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Self, cast

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

import tests.conftest as test_conftest
from tests.conftest import _RoleDsns
from tests.db.conftest import _MigratedWorkerDb


@pytest.mark.anyio
async def test_kdive_worker_pool_uses_login_while_owner_can_seed(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    kdive_worker_pool: AsyncConnectionPool,
) -> None:
    """The owner URL and worker pool stay available as distinct database principals."""
    with psycopg.connect(migrated_url) as owner:
        owner_user = owner.execute("SELECT current_user").fetchone()

    async with kdive_worker_pool.connection() as worker:
        worker_user = await (await worker.execute("SELECT current_user")).fetchone()

    assert owner_user is not None
    assert worker_user is not None
    assert worker_user[0] == authority_role_dsns.logins["kdive_worker"]
    assert worker_user[0] != owner_user[0]


def test_authority_role_login_lifecycle_holds_the_cluster_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOGIN creation and teardown serialize with migration-time role changes."""
    locks: list[str] = []
    statements: list[object] = []

    @contextmanager
    def lock(postgres_url: str) -> Iterator[None]:
        locks.append(postgres_url)
        yield

    class _Connection:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: object) -> None:
            statements.append(statement)

    monkeypatch.setattr(test_conftest, "_cluster_global_role_lock", lock)
    monkeypatch.setattr(test_conftest.psycopg, "connect", lambda *_args, **_kwargs: _Connection())

    fixture = cast(Any, test_conftest.authority_role_logins).__wrapped__(
        _MigratedWorkerDb(url="postgresql://worker", snapshot={}), "postgresql://admin"
    )
    logins = next(fixture)
    assert set(logins) == {
        "kdive_server",
        "kdive_worker",
        "kdive_reconciler",
        "kdive_provider_authority",
    }
    with pytest.raises(StopIteration):
        next(fixture)

    assert locks == ["postgresql://admin", "postgresql://admin"]
    assert len(statements) == 8
