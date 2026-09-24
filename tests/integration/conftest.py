"""Fixtures + shared helpers for the walking-skeleton integration tests (#26, ADR-0035).

Re-exports the disposable-Postgres fixtures (ADR-0015) so the non-gated exit-criterion
tests run against a freshly-migrated schema, and provides the `_pool` connection-pool
context manager and the `request_context` builder every test reuses — the same shapes the
per-plane MCP suites use, kept here so the integration module imports one place.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.auth import RequestContext
from kdive.security.authz.rbac import Role

# Re-export the disposable-Postgres fixtures so the integration tests can request them.
from tests.db.conftest import _migrated_db, migrated_url, pg_conn, postgres_url  # noqa: F401
from tests.integration.live_stack.skew import probe_stack_skew
from tests.store.conftest import minio_store  # noqa: F401


def pytest_report_header() -> list[str]:
    """Include probed app revisions in a live-stack proof's pytest header (#2752)."""
    base_url = os.environ.get("KDIVE_STACK_BASE_URL")
    if not base_url:
        return []
    probe = probe_stack_skew(base_url)
    revisions = [
        f"{result.process}="
        + (
            "not deployed"
            if not result.applicable
            else probe.revisions.get(result.process) or "unknown"
        )
        for result in probe.results
    ]
    return ["live-stack probed revisions: " + ", ".join(revisions)]


@asynccontextmanager
async def open_pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    """Yield an open async pool for ``url``, closed on exit."""
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def request_context(
    role: Role | None = Role.OPERATOR,
    *,
    principal: str = "user-1",
    projects: tuple[str, ...] = ("proj",),
) -> RequestContext:
    """Build a `RequestContext` granting ``role`` on the single test project (ADR-0035 §3)."""
    roles = {projects[0]: role} if role is not None else {}
    return RequestContext(
        principal=principal, agent_session="sess-1", projects=projects, roles=roles
    )
