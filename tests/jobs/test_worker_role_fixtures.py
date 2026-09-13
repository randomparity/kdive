"""Worker-role fixture coverage for handler authorization tests."""

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from tests.conftest import _RoleDsns


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
