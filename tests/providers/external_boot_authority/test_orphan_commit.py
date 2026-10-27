"""The orphan disposition commit must be durable on the host's non-autocommit connection (#3186)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from kdive.providers.external_boot_authority.orphan import RecoveryOrphanAuthorityService
from kdive.providers.external_boot_authority.service import AuthorityServiceError


class _Cursor:
    def __init__(self, outcome: str | None) -> None:
        self._outcome = outcome

    async def fetchone(self) -> tuple[str] | None:
        return None if self._outcome is None else (self._outcome,)


class _Connection:
    """A non-autocommit connection: work counts as committed only if made inside transaction()."""

    def __init__(self, outcome: str | None) -> None:
        self._outcome = outcome
        self._depth = 0
        self.committed = 0
        self.uncommitted = 0

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1

    async def execute(self, _sql: str, _params: object) -> _Cursor:
        if self._depth:
            self.committed += 1
        else:
            self.uncommitted += 1
        return _Cursor(self._outcome)


def _commit(conn: _Connection) -> None:
    @asynccontextmanager
    async def connections() -> AsyncIterator[_Connection]:
        yield conn

    service = RecoveryOrphanAuthorityService(cast(Any, connections), cast(Any, SimpleNamespace()))
    peer: Any = SimpleNamespace(incarnation_id=uuid4())
    request: Any = SimpleNamespace(request_id=uuid4(), job_id=uuid4(), job_attempt=1)
    selection: Any = SimpleNamespace(object_id=uuid4(), ownership_digest="sha256:" + "a" * 64)
    asyncio.run(service._commit(peer, request, selection, "sha256:" + "b" * 64))


def test_the_disposition_commit_runs_inside_its_own_transaction() -> None:
    conn = _Connection("applied")
    _commit(conn)
    assert (conn.committed, conn.uncommitted) == (1, 0)


def test_a_superseded_disposition_is_refused_without_a_partial_write() -> None:
    conn = _Connection(None)
    with pytest.raises(AuthorityServiceError):
        _commit(conn)
    assert conn.uncommitted == 0
