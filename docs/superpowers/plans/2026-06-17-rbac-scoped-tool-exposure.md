# RBAC-scoped tool exposure + usage tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Filter `list_tools` per connection so a caller sees only tools its grants could invoke, and record every tool call in a durable `tool_invocation` table for later usage analysis.

**Architecture:** Two new FastMCP middlewares wired into `build_app` after the existing three. `ToolExposureMiddleware.on_list_tools` filters the returned `Sequence[Tool]` using a central authorization map (`mcp/exposure.py`) against the connection's `RequestContext`. `UsageTrackingMiddleware.on_call_tool` records one best-effort `tool_invocation` row per call. Implements ADR-0148; spec `docs/design/tool-exposure-scoping.md`.

**Tech Stack:** Python 3.13, `uv`, FastMCP 3.4.0, psycopg/psycopg_pool, Postgres, pytest.

## Global Constraints

- Guardrails before every commit: `just lint` (ruff check + format check), `just type` (whole-tree `ty`), `just test` (excludes `live_vm`). CI runs these individually — a test in `tests/` is hard-gated by `just test`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. Absolute imports only (no `..`). Google-style docstrings on non-trivial public APIs.
- Error taxonomy: pick the most specific existing `ErrorCategory`; never invent strings.
- Doc/prose: plain factual language; never "critical/robust/comprehensive/elegant"; "Milestone" not "Sprint".
- Migrations are forward-only `schema/NNNN_*.sql`, immutable once applied (ADR-0015). Next free number is `0039`.
- The exposure filter is **advisory, fail-open** — never raise out of `on_list_tools`; on any error return the unfiltered list and log.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## File Structure

- `src/kdive/mcp/exposure.py` (create) — `ExposureScope` enum, the grouped classification sets, `visible_tool_names(ctx, all_names)` / `is_visible(scope, ctx)` pure functions. No FastMCP/DB imports.
- `src/kdive/mcp/middleware.py` (modify) — add `ToolExposureMiddleware` and `UsageTrackingMiddleware`.
- `src/kdive/security/usage.py` (create) — `UsageEvent` dataclass + `record_usage(conn, event)` writer (mirrors `security/audit.py`).
- `src/kdive/db/schema/0039_tool_invocation.sql` (create) — the append-only table.
- `src/kdive/mcp/app.py` (modify) — wire the two middlewares; `UsageTrackingMiddleware` needs the pool.
- Tests: `tests/mcp/core/test_exposure.py`, `tests/mcp/core/test_tool_exposure_middleware.py`, `tests/mcp/core/test_usage_tracking_middleware.py`, `tests/db/test_usage.py`, additions to `tests/mcp/core/test_app.py` (completeness guard) and `tests/integration/test_wire_harness.py` (transport-level reduction).

---

### Task 0: Spike — confirm the access token resolves in `on_list_tools`

**Files:**
- Test: `tests/mcp/core/test_tool_exposure_middleware.py` (create, spike test kept as a regression)

**Interfaces:**
- Produces: confirmation that `fastmcp.server.dependencies.get_access_token()` returns the verified token inside an `on_list_tools` middleware hook driven over the in-memory client with a bearer token. This gates whether the filter can read the caller's roles at all (spec "Prerequisite").

- [ ] **Step 1: Write a spike test that drives `list_tools` through a middleware that records whether a token was present**

```python
"""Spike + regression: the verified token resolves inside on_list_tools (#506)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware

from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair, mint


class _TokenProbe(Middleware):
    seen_sub: str | None = None

    async def on_list_tools(self, context: Any, call_next: Any) -> Sequence[Any]:
        token = get_access_token()
        _TokenProbe.seen_sub = None if token is None else token.claims.get("sub")
        return await call_next(context)


def test_access_token_resolves_in_on_list_tools() -> None:
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    app = FastMCP(name="probe", auth=verifier)

    @app.tool
    async def ping() -> dict[str, str]:
        return {"ok": "1"}

    probe = _TokenProbe()
    app.add_middleware(probe)
    token = mint(kp, sub="alice", projects=["proj-a"], roles={"proj-a": "viewer"})

    async def _run() -> None:
        async with Client(app, auth=token) as client:
            await client.list_tools()

    asyncio.run(_run())
    assert _TokenProbe.seen_sub == "alice"
```

- [ ] **Step 2: Run it**

Run: `uv run python -m pytest tests/mcp/core/test_tool_exposure_middleware.py::test_access_token_resolves_in_on_list_tools -q`
Expected: PASS. If it FAILS with `seen_sub is None`, STOP — the token does not resolve in `on_list_tools`; the design's filtering seam is invalid and the approach must be revisited (report as a blocker before writing the filter). Verify `mint`'s signature in `tests/mcp/conftest.py` and adapt the call (it mints a signed JWT with `projects`/`roles` claims).

- [ ] **Step 3: Commit the spike as a regression test**

```bash
git add tests/mcp/core/test_tool_exposure_middleware.py
git commit -m "test(mcp): confirm access token resolves in on_list_tools (#506)"
```

---

### Task 1: Exposure classification + visibility logic (`mcp/exposure.py`)

**Files:**
- Create: `src/kdive/mcp/exposure.py`
- Test: `tests/mcp/core/test_exposure.py` (create)

**Interfaces:**
- Consumes: `kdive.security.authz.rbac.Role`, `PlatformRole`, `_PLATFORM_IMPLIES`; `kdive.security.authz.context.RequestContext`.
- Produces:
  - `class ExposureScope(StrEnum)`: `PUBLIC`, `PROJECT_VIEWER`, `PROJECT_OPERATOR`, `PROJECT_ADMIN`, `PLATFORM_OPERATOR`, `PLATFORM_ADMIN`, `PLATFORM_AUDITOR`.
  - `def scope_for(tool_name: str) -> ExposureScope` — map lookup, default `PUBLIC`.
  - `def is_visible(scope: ExposureScope, ctx: RequestContext) -> bool`.
  - `def visible_tool_names(ctx: RequestContext, names: Iterable[str]) -> set[str]`.

- [ ] **Step 1: Write the failing test**

```python
"""Exposure classification + per-connection visibility rule (#506, ADR-0148)."""

from __future__ import annotations

from kdive.mcp.exposure import (
    ExposureScope,
    is_visible,
    scope_for,
    visible_tool_names,
)
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, Role


def _ctx(
    *, roles: dict[str, Role] | None = None, platform: frozenset[PlatformRole] = frozenset()
) -> RequestContext:
    roles = roles or {}
    return RequestContext(
        principal="p",
        agent_session=None,
        projects=tuple(roles),
        roles=roles,
        platform_roles=platform,
    )


def test_public_always_visible() -> None:
    assert is_visible(ExposureScope.PUBLIC, _ctx())


def test_project_role_union_rule() -> None:
    operator_here = _ctx(roles={"a": Role.VIEWER, "b": Role.OPERATOR})
    assert is_visible(ExposureScope.PROJECT_OPERATOR, operator_here)  # operator in b
    assert is_visible(ExposureScope.PROJECT_VIEWER, operator_here)
    assert not is_visible(ExposureScope.PROJECT_ADMIN, operator_here)


def test_viewer_only_hides_operator_and_admin() -> None:
    viewer = _ctx(roles={"a": Role.VIEWER})
    assert is_visible(ExposureScope.PROJECT_VIEWER, viewer)
    assert not is_visible(ExposureScope.PROJECT_OPERATOR, viewer)
    assert not is_visible(ExposureScope.PROJECT_ADMIN, viewer)


def test_platform_admin_implies_auditor() -> None:
    admin = _ctx(platform=frozenset({PlatformRole.PLATFORM_ADMIN}))
    assert is_visible(ExposureScope.PLATFORM_AUDITOR, admin)
    assert is_visible(ExposureScope.PLATFORM_ADMIN, admin)
    assert not is_visible(ExposureScope.PLATFORM_OPERATOR, admin)  # not implied (ADR-0043)


def test_no_grants_sees_only_public() -> None:
    bare = _ctx()
    names = {"jobs.get", "allocations.request", "control.power", "ops.reconcile_now"}
    # jobs.get is PUBLIC; the rest are gated.
    assert visible_tool_names(bare, names) == {"jobs.get"}


def test_unclassified_defaults_public() -> None:
    assert scope_for("some.brand_new_tool") is ExposureScope.PUBLIC
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/core/test_exposure.py -q`
Expected: FAIL with `ModuleNotFoundError: kdive.mcp.exposure`.

- [ ] **Step 3: Implement `mcp/exposure.py`**

Classify tools by their handler's real `require_role` / `require_platform_role` requirement. Seed the sets from the destructive set and the per-plane RBAC docstrings; the completeness guard (Task 4) forces any unlisted tool to be triaged. Use the verbatim tool names. Group by the highest distinct gate.

```python
"""Per-connection tool-exposure classification + visibility rule (ADR-0148, #506).

`list_tools` is connection-scoped while project roles are per-project, so a tool is
visible iff the caller could invoke it under *some* grant: the union of project roles
plus the connection's platform roles. Classification is a central reviewed map (the
`_docmeta.DESTRUCTIVE_TOOLS` idiom); an unclassified tool defaults to `PUBLIC`
(fail-open) and the completeness guard test forces it to be triaged. The filter is
advisory — not a security control; execution-time RBAC remains the enforcement.

A classification must be <= the handler's real requirement: too-permissive only costs
catalog size, too-restrictive hides a usable tool.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import _PLATFORM_IMPLIES, PlatformRole, Role

_ROLE_RANK: dict[Role, int] = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}


class ExposureScope(StrEnum):
    """The authorization a caller needs before a tool is worth advertising."""

    PUBLIC = "public"
    PROJECT_VIEWER = "project_viewer"
    PROJECT_OPERATOR = "project_operator"
    PROJECT_ADMIN = "project_admin"
    PLATFORM_OPERATOR = "platform_operator"
    PLATFORM_ADMIN = "platform_admin"
    PLATFORM_AUDITOR = "platform_auditor"


_PROJECT_SCOPE: dict[ExposureScope, Role] = {
    ExposureScope.PROJECT_VIEWER: Role.VIEWER,
    ExposureScope.PROJECT_OPERATOR: Role.OPERATOR,
    ExposureScope.PROJECT_ADMIN: Role.ADMIN,
}
_PLATFORM_SCOPE: dict[ExposureScope, PlatformRole] = {
    ExposureScope.PLATFORM_OPERATOR: PlatformRole.PLATFORM_OPERATOR,
    ExposureScope.PLATFORM_ADMIN: PlatformRole.PLATFORM_ADMIN,
    ExposureScope.PLATFORM_AUDITOR: PlatformRole.PLATFORM_AUDITOR,
}

# Reviewed classification. Each set lists tools whose handler enforces at least the
# named scope (verbatim tool names). Anything absent defaults to PUBLIC; the
# completeness guard (tests/mcp/core/test_app.py) forces new tools to be triaged here.
# NOTE: keep <= the handler's real require_role; the actual map is filled to match
# the per-plane RBAC docstrings during implementation.
_SCOPE_SETS: tuple[tuple[ExposureScope, frozenset[str]], ...] = (
    (ExposureScope.PLATFORM_ADMIN, frozenset({...})),
    (ExposureScope.PLATFORM_OPERATOR, frozenset({...})),
    (ExposureScope.PLATFORM_AUDITOR, frozenset({...})),
    (ExposureScope.PROJECT_ADMIN, frozenset({...})),
    (ExposureScope.PROJECT_OPERATOR, frozenset({...})),
    (ExposureScope.PROJECT_VIEWER, frozenset({...})),
)
_SCOPE_BY_TOOL: dict[str, ExposureScope] = {
    name: scope for scope, names in _SCOPE_SETS for name in names
}


def scope_for(tool_name: str) -> ExposureScope:
    """Return the reviewed scope for ``tool_name``; ``PUBLIC`` if unclassified."""
    return _SCOPE_BY_TOOL.get(tool_name, ExposureScope.PUBLIC)


def _max_project_rank(ctx: RequestContext) -> int:
    return max((_ROLE_RANK[r] for r in ctx.roles.values()), default=-1)


def _has_platform(ctx: RequestContext, needed: PlatformRole) -> bool:
    for held in ctx.platform_roles:
        if held is needed or needed in _PLATFORM_IMPLIES.get(held, frozenset()):
            return True
    return False


def is_visible(scope: ExposureScope, ctx: RequestContext) -> bool:
    """Whether a tool of ``scope`` could be invoked by ``ctx`` under some grant."""
    if scope is ExposureScope.PUBLIC:
        return True
    project_role = _PROJECT_SCOPE.get(scope)
    if project_role is not None:
        return _max_project_rank(ctx) >= _ROLE_RANK[project_role]
    platform_role = _PLATFORM_SCOPE[scope]
    return _has_platform(ctx, platform_role)


def visible_tool_names(ctx: RequestContext, names: Iterable[str]) -> set[str]:
    """The subset of ``names`` visible to ``ctx``."""
    return {name for name in names if is_visible(scope_for(name), ctx)}
```

Note: importing the private `_PLATFORM_IMPLIES` is acceptable (same security package family); if `ty` or review objects, promote it to a public helper `platform_role_satisfies(held, needed)` in `rbac.py` and call that instead.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/mcp/core/test_exposure.py -q`
Expected: PASS. (Fill the `{...}` sets in Task 4 once the live tool list is known; the logic tests above pass with the explicit names they pass in.)

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/exposure.py tests/mcp/core/test_exposure.py
git commit -m "feat(mcp): add tool-exposure classification + visibility rule (#506)"
```

---

### Task 2: `ToolExposureMiddleware` (filter `on_list_tools`)

**Files:**
- Modify: `src/kdive/mcp/middleware.py`
- Test: `tests/mcp/core/test_tool_exposure_middleware.py` (extend the Task 0 file)

**Interfaces:**
- Consumes: `mcp.exposure.visible_tool_names`, `mcp.auth.current_context`, `security.authz.errors.AuthError`.
- Produces: `class ToolExposureMiddleware(Middleware)` with `async def on_list_tools(self, context, call_next) -> Sequence[Tool]`.

- [ ] **Step 1: Write failing tests (unit + fail-open)**

```python
import logging
from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from kdive.mcp.middleware import ToolExposureMiddleware
from kdive.security.authz.context import RequestContext
from kdive.security.authz.errors import AuthError
from kdive.security.authz.rbac import Role


@dataclass
class _T:
    name: str


_ALL = [_T("jobs.get"), _T("allocations.request"), _T("control.power"), _T("ops.reconcile_now")]


async def _passthrough(_ctx: object) -> Sequence[_T]:
    return _ALL


def _viewer_ctx() -> RequestContext:
    return RequestContext(
        principal="v", agent_session=None, projects=("a",), roles={"a": Role.VIEWER}
    )


def test_viewer_list_is_reduced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kdive.mcp.middleware.current_context", _viewer_ctx)
    mw = ToolExposureMiddleware()
    out = asyncio_run(mw.on_list_tools(object(), _passthrough))
    names = {t.name for t in out}
    assert "jobs.get" in names
    assert "control.power" not in names and "ops.reconcile_now" not in names
    assert names < {t.name for t in _ALL}


def test_fail_open_when_no_context(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    def _raise() -> RequestContext:
        raise AuthError("no token")

    monkeypatch.setattr("kdive.mcp.middleware.current_context", _raise)
    mw = ToolExposureMiddleware()
    with caplog.at_level(logging.WARNING):
        out = asyncio_run(mw.on_list_tools(object(), _passthrough))
    assert {t.name for t in out} == {t.name for t in _ALL}  # unfiltered
```

(Define a small `asyncio_run` helper or use `asyncio.run`. Note: classification of `allocations.request` as `PROJECT_OPERATOR` and `control.power`/`ops.reconcile_now` as gated must be set in `exposure.py` for this assertion; align with Task 1's `_SCOPE_SETS`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/core/test_tool_exposure_middleware.py -q`
Expected: FAIL (`ToolExposureMiddleware` undefined).

- [ ] **Step 3: Implement the middleware**

Add to `mcp/middleware.py` (import `Sequence`, `Tool`, `current_context`, `AuthError`, `visible_tool_names` at top):

```python
class ToolExposureMiddleware(Middleware):
    """Filter `list_tools` to the tools the connection's grants could invoke (ADR-0148).

    Advisory, **fail-open**: list filtering is an accuracy aid, not a security control —
    execution-time RBAC remains the boundary. On a missing/invalid context or any
    internal error it returns the unfiltered catalog and logs, so tool discovery never
    breaks.
    """

    async def on_list_tools(
        self,
        context: Any,
        call_next: Callable[[Any], Any],
    ) -> Sequence[Tool]:
        tools: Sequence[Tool] = await call_next(context)
        try:
            ctx = current_context()
            visible = visible_tool_names(ctx, (t.name for t in tools))
        except Exception:  # advisory filter: never break discovery
            _log.warning("tool-exposure filter failed; advertising full catalog", exc_info=True)
            return tools
        return [t for t in tools if t.name in visible]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/mcp/core/test_tool_exposure_middleware.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/middleware.py tests/mcp/core/test_tool_exposure_middleware.py
git commit -m "feat(mcp): filter list_tools by connection grants (#506)"
```

---

### Task 3: `tool_invocation` table + `record_usage` writer

**Files:**
- Create: `src/kdive/db/schema/0039_tool_invocation.sql`
- Create: `src/kdive/security/usage.py`
- Test: `tests/db/test_usage.py` (create)

**Interfaces:**
- Produces:
  - table `tool_invocation(id, ts, principal, agent_session, project, tool, outcome, actor, client_id)`.
  - `@dataclass(frozen=True, slots=True) class UsageEvent` with `principal: str`, `agent_session: str | None`, `project: str | None`, `tool: str`, `outcome: str`, `actor: str`, `client_id: str | None`.
  - `async def record_usage(conn: AsyncConnection, event: UsageEvent) -> UUID`.

- [ ] **Step 1: Write the migration**

```sql
-- 0039_tool_invocation.sql — per-call usage analytics (ADR-0148, #506).
-- Append-only, modelled on platform_audit_log: no project-membership guard (list-time
-- and object-resolving calls may carry no resolvable project, so `project` is nullable).
-- This is operational analytics, NOT an audit trail (no args_digest) and distinct from
-- audit_log/platform_audit_log. `outcome` is CHECK-constrained to the closed set so a
-- bad classification fails loud. `actor` reuses the operator-cli|agent|unknown
-- classification (ADR-0089), NOT NULL with a default so the column is total.
CREATE TABLE tool_invocation (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ts            timestamptz NOT NULL DEFAULT now(),
    principal     text NOT NULL,
    agent_session text,
    project       text,
    tool          text NOT NULL,
    outcome       text NOT NULL CHECK (outcome IN ('ok', 'error', 'denied')),
    actor         text NOT NULL DEFAULT 'agent',
    client_id     text
);

CREATE INDEX tool_invocation_tool_ts_idx ON tool_invocation (tool, ts);
```

- [ ] **Step 2: Write the failing writer test**

```python
"""tool_invocation writer (#506, ADR-0148)."""

from __future__ import annotations

import asyncio

import psycopg

from kdive.security.usage import UsageEvent, record_usage


def test_record_usage_writes_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            rid = await record_usage(
                conn,
                UsageEvent(
                    principal="alice",
                    agent_session="s1",
                    project="proj-a",
                    tool="jobs.get",
                    outcome="ok",
                    actor="agent",
                    client_id=None,
                ),
            )
            await conn.commit()
            cur = await conn.execute(
                "SELECT principal, tool, outcome, project FROM tool_invocation WHERE id=%s",
                (rid,),
            )
            row = await cur.fetchone()
        assert row == ("alice", "jobs.get", "ok", "proj-a")

    asyncio.run(_run())


def test_record_usage_rejects_bad_outcome(migrated_url: str) -> None:
    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            try:
                await record_usage(
                    conn,
                    UsageEvent(
                        principal="a", agent_session=None, project=None,
                        tool="t", outcome="bogus", actor="agent", client_id=None,
                    ),
                )
                await conn.commit()
                raise AssertionError("expected CHECK violation")
            except psycopg.errors.CheckViolation:
                pass

    asyncio.run(_run())
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run python -m pytest tests/db/test_usage.py -q`
Expected: FAIL (`kdive.security.usage` missing). Requires Docker (testcontainers Postgres); if Docker is absent it SKIPs — run with Docker available.

- [ ] **Step 4: Implement `security/usage.py`**

```python
"""Append-only per-call usage analytics writer (ADR-0148, #506).

`record_usage` writes one `tool_invocation` row. Operational analytics, not an audit
trail: no membership guard and no args_digest (distinct from `security/audit.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from psycopg import AsyncConnection


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """One dispatched tool call's recorded dimensions."""

    principal: str
    agent_session: str | None
    project: str | None
    tool: str
    outcome: str
    actor: str
    client_id: str | None


async def record_usage(conn: AsyncConnection, event: UsageEvent) -> UUID:
    """Append one `tool_invocation` row; return its id.

    Runs the INSERT on ``conn`` without opening a transaction, so the caller controls
    commit. ``outcome`` is CHECK-constrained at the DB to ``ok|error|denied``.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO tool_invocation "
            "(principal, agent_session, project, tool, outcome, actor, client_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                event.principal,
                event.agent_session,
                event.project,
                event.tool,
                event.outcome,
                event.actor,
                event.client_id,
            ),
        )
        row = await cur.fetchone()
    if row is None:  # Invariant: INSERT ... RETURNING always yields one row.
        raise RuntimeError("INSERT into tool_invocation returned no row")
    return row[0]
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run python -m pytest tests/db/test_usage.py -q`
Expected: PASS (with Docker).

- [ ] **Step 6: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/db/schema/0039_tool_invocation.sql src/kdive/security/usage.py tests/db/test_usage.py
git commit -m "feat(db): add tool_invocation usage table + writer (#506)"
```

---

### Task 4: `UsageTrackingMiddleware` (record each call) + outcome classification

**Files:**
- Modify: `src/kdive/mcp/middleware.py`
- Test: `tests/mcp/core/test_usage_tracking_middleware.py` (create)

**Interfaces:**
- Consumes: `record_usage`, `UsageEvent`, `current_context`, `mcp.responses.ToolResponse`, `domain.errors.ErrorCategory`, `security.authz.rbac.AuthorizationError`, `security.authz.actor.actor_from_client_id` (the existing actor classifier — verify its exact name/signature in `src/kdive/security/authz/actor.py`).
- Produces: `class UsageTrackingMiddleware(Middleware)` taking `pool: AsyncConnectionPool` and an optional `acquire_timeout: float = 1.0`.

- [ ] **Step 1: Write failing tests (one per outcome + best-effort swallow)**

```python
"""Per-call usage recording + outcome classification (#506, ADR-0148)."""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.middleware import UsageTrackingMiddleware
from kdive.mcp.responses import ToolResponse
from kdive.security.authz.context import RequestContext
from kdive.security.authz.gate import DestructiveOpDenied
from kdive.security.authz.rbac import Role


def _ctx() -> RequestContext:
    return RequestContext(
        principal="alice", agent_session="s1", projects=("a",), roles={"a": Role.OPERATOR}
    )


class _Ctx:
    def __init__(self, tool: str) -> None:
        self.message = type("M", (), {"name": tool, "arguments": {}})()


def _outcomes(migrated_url: str, tool: str, behavior, monkeypatch) -> list[tuple[Any, ...]]:
    monkeypatch.setattr("kdive.mcp.middleware.current_context", _ctx)

    async def _run() -> list[tuple[Any, ...]]:
        async with AsyncConnectionPool(migrated_url, open=False) as pool:
            await pool.open()
            mw = UsageTrackingMiddleware(pool)
            try:
                await mw.on_call_tool(_Ctx(tool), behavior)
            except Exception:
                pass
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT tool, outcome, principal FROM tool_invocation"
                )
                return await cur.fetchall()

    return asyncio.run(_run())


def test_ok_outcome(migrated_url, monkeypatch) -> None:
    async def ok(_c): return ToolResponse.success("jobs.get")
    rows = _outcomes(migrated_url, "jobs.get", ok, monkeypatch)
    assert rows == [("jobs.get", "ok", "alice")]


def test_denied_via_envelope(migrated_url, monkeypatch) -> None:
    async def denied(_c):
        return ToolResponse.failure("x", ErrorCategory.AUTHORIZATION_DENIED)
    rows = _outcomes(migrated_url, "x", denied, monkeypatch)
    assert rows[0][1] == "denied"


def test_denied_via_propagated_exception(migrated_url, monkeypatch) -> None:
    async def boom(_c): raise DestructiveOpDenied("nope")
    rows = _outcomes(migrated_url, "control.power", boom, monkeypatch)
    assert rows[0][1] == "denied"


def test_error_outcome(migrated_url, monkeypatch) -> None:
    async def err(_c): raise RuntimeError("boom")
    rows = _outcomes(migrated_url, "y", err, monkeypatch)
    assert rows[0][1] == "error"


def test_recording_failure_is_swallowed(migrated_url, monkeypatch) -> None:
    # A closed pool makes recording fail; the success result must still return.
    monkeypatch.setattr("kdive.mcp.middleware.current_context", _ctx)

    async def _run():
        pool = AsyncConnectionPool(migrated_url, open=False)  # never opened
        mw = UsageTrackingMiddleware(pool, acquire_timeout=0.05)
        async def ok(_c): return ToolResponse.success("jobs.get")
        return await mw.on_call_tool(_Ctx("jobs.get"), ok)

    result = asyncio.run(_run())
    assert result is not None  # call result unaffected by the recording failure
```

Verify `ToolResponse.success(...)` / `.failure(...)` constructor names against `src/kdive/mcp/responses.py`; adapt if the helpers differ. Verify how to read the returned envelope's `error_category` (the middleware sees a `ToolResult` whose `structured_content` is the envelope dict, per ADR-0113 — read `error_category` from there; the in-body `ToolResponse` return path may differ. Inspect what `call_next` actually returns in this middleware position and key the classification off that).

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/core/test_usage_tracking_middleware.py -q`
Expected: FAIL (`UsageTrackingMiddleware` undefined). Requires Docker.

- [ ] **Step 3: Implement the middleware**

```python
class UsageTrackingMiddleware(Middleware):
    """Record one best-effort `tool_invocation` row per call (ADR-0148, #506).

    Best-effort: a recording failure (or a saturated pool past ``acquire_timeout``) is
    logged and swallowed — it never fails or delays the call. Sits just inside
    `TelemetryMiddleware`, so it observes the final outcome after `DenialAuditMiddleware`
    converts a role/membership denial to an `authorization_denied` envelope; a propagated
    `AuthorizationError` (its `DestructiveOpDenied` subclass and the base non-member
    denial) is classified `denied` too, so the denial signal is complete.
    """

    def __init__(self, pool: AsyncConnectionPool, *, acquire_timeout: float = 1.0) -> None:
        self._pool = pool
        self._acquire_timeout = acquire_timeout

    async def on_call_tool(self, context: Any, call_next: Callable[[Any], Any]) -> Any:
        try:
            result = await call_next(context)
        except AuthorizationError:
            await self._record(context, "denied")
            raise
        except Exception:
            await self._record(context, "error")
            raise
        await self._record(context, self._classify(result))
        return result

    @staticmethod
    def _classify(result: Any) -> str:
        category = _result_error_category(result)
        if category is None:
            return "ok"
        if category == ErrorCategory.AUTHORIZATION_DENIED.value:
            return "denied"
        return "error"

    async def _record(self, context: Any, outcome: str) -> None:
        try:
            ctx = current_context()
            event = UsageEvent(
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                project=_arg_project(context),
                tool=context.message.name,
                outcome=outcome,
                actor=actor_from_client_id(ctx.client_id),
                client_id=ctx.client_id,
            )
            async with self._pool.connection(timeout=self._acquire_timeout) as conn, \
                    conn.transaction():
                await record_usage(conn, event)
        except Exception:
            _log.warning("usage recording failed for tool", exc_info=True)
```

Add module-private helpers `_result_error_category(result) -> str | None` (reads the envelope's `error_category` from the `ToolResult.structured_content` dict, returning `None` on success or when the shape is unrecognised) and `_arg_project(context) -> str | None` (reads `arguments.get("project")` if a non-empty str). Verify `actor_from_client_id` exists; if the public name differs, use the real one from `security/authz/actor.py`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m pytest tests/mcp/core/test_usage_tracking_middleware.py -q`
Expected: PASS (with Docker).

- [ ] **Step 5: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/middleware.py tests/mcp/core/test_usage_tracking_middleware.py
git commit -m "feat(mcp): record per-call usage with outcome classification (#506)"
```

---

### Task 5: Wire both middlewares into `build_app` + completeness guard

**Files:**
- Modify: `src/kdive/mcp/app.py:404-414` (middleware registration block)
- Modify: `tests/mcp/core/test_app.py` (add completeness guard)
- Modify: `src/kdive/mcp/exposure.py` (fill `_SCOPE_SETS` with the real tool names)

**Interfaces:**
- Consumes: `ToolExposureMiddleware`, `UsageTrackingMiddleware` from `mcp.middleware`.

- [ ] **Step 1: Write the completeness guard test (it will fail until `_SCOPE_SETS` is filled)**

```python
def test_every_tool_is_classified_or_public() -> None:
    """Every registered tool resolves to a scope; gated tools must be in the map.

    Forces a new privileged tool to be triaged: any tool whose name is NOT in the
    exposure map defaults to PUBLIC, so this test pins the *known gated* tools into the
    map and fails if one drops out (a silent un-gating). PUBLIC tools need no entry.
    """
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())

    async def _run() -> set[str]:
        return {t.name for t in await app.list_tools()}

    names = asyncio.run(_run())
    # Spot-pin: these MUST stay gated (not silently PUBLIC).
    from kdive.mcp.exposure import ExposureScope, scope_for

    assert scope_for("control.power") is ExposureScope.PROJECT_ADMIN
    assert scope_for("ops.reconcile_now") in {
        ExposureScope.PLATFORM_OPERATOR, ExposureScope.PLATFORM_ADMIN
    }
    assert scope_for("allocations.request") is ExposureScope.PROJECT_OPERATOR
    # Every classified name is a real registered tool (no stale entries).
    from kdive.mcp.exposure import _SCOPE_BY_TOOL

    assert set(_SCOPE_BY_TOOL) <= names
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/core/test_app.py::test_every_tool_is_classified_or_public -q`
Expected: FAIL (placeholder `{...}` sets / stale entries).

- [ ] **Step 3: Fill `_SCOPE_SETS` from the live tool list + per-plane RBAC**

Enumerate the live tools and their handler gates:

```bash
uv run python -c "
import asyncio
from psycopg_pool import AsyncConnectionPool
from kdive.mcp.app import build_app
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import make_keypair, ISSUER, AUDIENCE
from fastmcp.server.auth.providers.jwt import JWTVerifier
kp=make_keypair(); v=JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
app=build_app(AsyncConnectionPool('postgresql://unused', open=False), verifier=v, secret_registry=SecretRegistry())
print('\n'.join(sorted(t.name for t in asyncio.run(app.list_tools()))))
"
```

For each tool, read its registrar's `require_role` / `require_platform_role` call and assign the matching `ExposureScope` (`<=` the real requirement). Reads requiring only project `viewer` may stay `PROJECT_VIEWER` or `PUBLIC` (a no-project caller seeing a viewer-gated read it can't use costs only catalog size; prefer `PROJECT_VIEWER` so a bare token's catalog is genuinely minimal). Cross-check against `_docmeta.DESTRUCTIVE_TOOLS` (all destructive tools are at least `PROJECT_ADMIN` or platform-scoped). Replace every `{...}`.

- [ ] **Step 4: Wire the middlewares in `build_app`**

In `app.py`, after `app.add_middleware(BindingErrorMiddleware())` add the two new ones. `TelemetryMiddleware` stays outermost (added first); add `UsageTrackingMiddleware` right after it and `ToolExposureMiddleware` after that:

```python
    app.add_middleware(
        TelemetryMiddleware(
            tracer=trace.get_tracer("kdive.mcp"), meter=metrics.get_meter("kdive.mcp")
        )
    )
    app.add_middleware(UsageTrackingMiddleware(pool))
    app.add_middleware(ToolExposureMiddleware())
    app.add_middleware(DenialAuditMiddleware(pool))
    app.add_middleware(BindingErrorMiddleware())
```

(Ordering note: `on_call_tool` runs added-order on the way in; `UsageTrackingMiddleware` added before `DenialAuditMiddleware` sees the denial *envelope* on the way out — exactly what `_classify` needs. `ToolExposureMiddleware` only hooks `on_list_tools`, so its position among the `on_call_tool` chain is immaterial. Import both new classes at the top of `app.py`.)

- [ ] **Step 5: Run the app + exposure + middleware tests**

Run: `uv run python -m pytest tests/mcp/core/test_app.py tests/mcp/core/test_exposure.py tests/mcp/core/test_tool_exposure_middleware.py -q`
Expected: PASS.

- [ ] **Step 6: Guardrails + commit**

```bash
just lint && just type
git add src/kdive/mcp/app.py src/kdive/mcp/exposure.py tests/mcp/core/test_app.py
git commit -m "feat(mcp): wire exposure + usage middlewares; classify all tools (#506)"
```

---

### Task 6: Transport-level reduction test (closes the silent-no-op gap)

**Files:**
- Modify: `tests/integration/test_wire_harness.py` (or create `tests/integration/test_list_tools_scoping.py` following its token-minting pattern)

**Interfaces:**
- Consumes: the wire harness's app build + token minting (`tests/mcp/conftest.py` `mint`, `tests/mcp/roles.py`).

- [ ] **Step 1: Write the failing transport test**

Drive the real in-memory client (or wire harness) with a minted **viewer** token and a minted **platform_admin+admin** token; assert the viewer's `list_tools` is strictly smaller and excludes a known gated tool, while the privileged token sees it.

```python
def test_viewer_list_tools_is_reduced_over_transport() -> None:
    # Build the app with a real JWTVerifier and drive list_tools with a viewer token.
    # (Mirror the harness in tests/integration/test_wire_harness.py for app+client setup.)
    ...
    viewer_names = {...}   # from client.list_tools() with viewer token
    admin_names = {...}    # with platform_admin+admin token
    assert "control.power" in admin_names
    assert "control.power" not in viewer_names
    assert viewer_names < admin_names
```

Fill `...` by reusing the harness's exact app-construction + `Client(app, auth=token)` pattern. This is the criterion-1 transport assertion that proves the token resolves in `on_list_tools` in the real dispatch path.

- [ ] **Step 2: Run to verify it fails, then passes once wired**

Run: `uv run python -m pytest tests/integration/test_wire_harness.py -q` (or the new file)
Expected: FAIL first if assertions are wrong, PASS after confirming behaviour.

- [ ] **Step 3: Guardrails + commit**

```bash
just lint && just type
git add tests/integration/
git commit -m "test(mcp): transport-level list_tools reduction for viewer token (#506)"
```

---

### Task 7: Full suite, ADR status, regenerate any tool reference

**Files:**
- Modify: `docs/adr/0148-rbac-scoped-tool-exposure.md` (status → Accepted on merge — leave Proposed until then)
- Possibly regenerate: the generated tool guide (ADR-0047) if it enumerates tools.

- [ ] **Step 1: Run the FULL suite (not just touched dirs)**

Run: `just lint && just type && just test`
Expected: all green. Architecture/boundary tests (e.g. `tests/mcp/core/test_tool_wrapper_boundary.py`, `test_tool_docs.py`) and any generated-doc check live outside the dirs touched — a full run catches them.

- [ ] **Step 2: Check for a generated tool reference that lists tools**

```bash
rg -l "generated" docs/ | rg -i tool | head
```

If `list_tools`-derived docs exist and a generator recipe exists (ADR-0047), regenerate and review. The exposure filter changes per-connection listing, not the registry, so the full unfiltered guide should be unchanged — confirm it is.

- [ ] **Step 3: Commit any regenerated artifacts**

```bash
git add -A && git commit -m "docs(mcp): regenerate tool reference after #506" # only if changed
```

---

## Self-Review

**Spec coverage:**
- Goal 1 (filtered list_tools) → Tasks 1, 2, 5, 6. ✓
- Goal 2 (usage capture) → Tasks 3, 4, 5. ✓
- Prerequisite (token in on_list_tools) → Task 0 (spike gate). ✓
- Conservative union rule → Task 1 tests. ✓
- Authorization map + completeness guard → Tasks 1, 5. ✓
- Fail-open failure modes → Task 2 fail-open test. ✓
- denied taxonomy incl. propagated AuthorizationError → Task 4. ✓
- Bounded recorder acquire / best-effort swallow → Task 4 (`acquire_timeout`, swallow test). ✓
- Success criterion 1 transport assertion → Task 6. ✓
- CHECK-constrained outcome → Task 3. ✓

**Placeholder scan:** The only intentional placeholders are the `{...}` scope sets (Task 1) and `...` (Task 6), each with an explicit fill step (Task 5 Step 3, Task 6 Step 1) and the command to enumerate the real names. No "TODO/handle edge cases" placeholders.

**Type consistency:** `ExposureScope`, `is_visible`, `scope_for`, `visible_tool_names`, `UsageEvent`, `record_usage`, `ToolExposureMiddleware`, `UsageTrackingMiddleware(pool, acquire_timeout=...)` are used with the same signatures across tasks. The two names to verify against the codebase during implementation are `actor_from_client_id` (security/authz/actor.py) and the `ToolResponse` success/failure constructors + how the envelope/`error_category` is read from the middleware's `call_next` result — both are flagged inline.
