"""Contract test: the real onboarding helper against the live accounting.* tools.

``scripts/kdive_set_accounting.py`` carries hard-coded tool names and parameter shapes
(``build_calls``); every other test for it stubs the transport, so those literals are only
ever asserted against themselves. This drives the *real* helper through the in-memory
``build_app`` so a rename of ``accounting.set_quota`` (or one of its params) breaks CI here
instead of a user's first ``allocations.request`` (#497).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

from fastmcp import Client
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

import scripts.kdive_set_accounting as acct
from kdive.db.repositories import BUDGETS, QUOTAS
from kdive.mcp.assembly.app import build_app
from kdive.mcp.tools.accounting import admin as accounting_admin
from kdive.mcp.tools.accounting import usage as accounting_usage
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair


def _verifier() -> JWTVerifier:
    keypair = make_keypair()
    return JWTVerifier(public_key=keypair.public_key, issuer=ISSUER, audience=AUDIENCE)


def _ctx(role: Role | None = Role.ADMIN) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="admin-1", agent_session="s", projects=("proj",), roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _patch_in_memory(monkeypatch, app, role: Role | None) -> None:
    # set_quota/set_budget bind current_context in `admin`; usage_project binds it in `usage`.
    monkeypatch.setattr(accounting_admin, "current_context", lambda: _ctx(role))
    monkeypatch.setattr(accounting_usage, "current_context", lambda: _ctx(role))
    # The helper builds a StreamableHttpTransport then Client(transport); redirect to the
    # in-memory app (auth comes from the patched current_context, so the Bearer is irrelevant).
    monkeypatch.setattr(acct, "Client", lambda transport: Client(app))


def test_helper_onboards_project_via_real_tools(migrated_url: str, monkeypatch) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
            _patch_in_memory(monkeypatch, app, Role.ADMIN)
            ns = acct.parse(["--base", "http://unused/mcp", "--token", "t", "--project", "proj"])
            rc = await acct.run(ns)
            assert rc == 0
            async with pool.connection() as conn:
                budget = await BUDGETS.get(conn, "proj")
                quota = await QUOTAS.get(conn, "proj")
        assert budget is not None
        assert budget.limit_kcu == Decimal("1000000")
        assert quota is not None
        assert quota.max_concurrent_allocations == 4
        assert quota.max_concurrent_systems == 4

    asyncio.run(_run())


def test_helper_returns_nonzero_when_unauthorized(migrated_url: str, monkeypatch) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
            _patch_in_memory(monkeypatch, app, Role.VIEWER)
            ns = acct.parse(["--base", "http://unused/mcp", "--token", "t", "--project", "proj"])
            rc = await acct.run(ns)
            assert rc == 1  # set_quota is admin-gated; a viewer is denied
            async with pool.connection() as conn:
                quota = await QUOTAS.get(conn, "proj")
        assert quota is None  # nothing written when the first tool is refused

    asyncio.run(_run())
