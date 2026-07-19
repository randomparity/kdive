"""Direct unit tests for the shared platform-audited audit read pagination."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import encode_ts_uuid_cursor
from kdive.mcp.tools.ops.audit import read_pipeline
from kdive.mcp.tools.ops.audit.read_pipeline import _response, query_platform_audited_page
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError


def _row(value: str, *, ts: object, row_id: object) -> dict[str, object]:
    return {"value": value, "ts": ts, "id": row_id}


def _row_data(row: dict[str, object]) -> dict[str, str]:
    return {"value": str(row["value"])}


def _row_object_id(row: dict[str, object], data: dict[str, str]) -> str:
    return data["value"]


def _build(rows: list[dict[str, object]], limit: int) -> ToolResponse:
    return _response(
        rows,
        limit,
        tool="audit.read",
        object_id="obj",
        list_tag="audit",
        row_data=_row_data,
        row_object_id=_row_object_id,
    )


def test_response_renders_items_without_a_cursor_when_not_truncated() -> None:
    now = datetime.now(UTC)
    rows = [_row("a", ts=now, row_id=uuid4()), _row("b", ts=now, row_id=uuid4())]
    resp = _build(rows, 5)
    assert resp.data["truncated"] is False
    assert resp.data["next_cursor"] is None
    assert resp.data["count"] == 2
    assert [item.object_id for item in resp.items] == ["a", "b"]
    assert resp.items[0].data == {"value": "a"}
    assert resp.suggested_next_actions == ["audit.read"]


def test_response_emits_a_keyset_cursor_when_truncated() -> None:
    now = datetime.now(UTC)
    last_kept_id = uuid4()
    rows = [
        _row("a", ts=now, row_id=uuid4()),
        _row("b", ts=now, row_id=last_kept_id),
        _row("c", ts=now, row_id=uuid4()),
    ]
    resp = _build(rows, 2)
    assert resp.data["truncated"] is True
    assert resp.data["count"] == 2
    assert resp.data["next_cursor"] == encode_ts_uuid_cursor("audit", now, last_kept_id)


def test_response_omits_cursor_when_anchor_row_lacks_typed_keys() -> None:
    rows = [
        _row("a", ts="not-a-datetime", row_id="not-a-uuid"),
        _row("b", ts="not-a-datetime", row_id="not-a-uuid"),
        _row("c", ts="not-a-datetime", row_id="not-a-uuid"),
    ]
    resp = _build(rows, 2)
    assert resp.data["truncated"] is True
    assert resp.data["next_cursor"] is None


def test_query_denies_and_read_audits_a_non_auditor(monkeypatch: pytest.MonkeyPatch) -> None:
    denials: list[tuple[object, object, str, dict[str, object]]] = []

    def _deny(ctx: object, role: object) -> None:
        raise AuthorizationError("not an auditor")

    async def _fake_audit_denial(
        pool: object, ctx: object, *, tool: str, args: dict[str, object]
    ) -> None:
        denials.append((pool, ctx, tool, args))

    async def _fetch_rows(conn: object, limit: int, after: object) -> list[dict[str, object]]:
        raise AssertionError("fetch must not run on a denied read")

    monkeypatch.setattr(read_pipeline, "require_platform_role", _deny)
    monkeypatch.setattr(read_pipeline._reads, "audit_denial", _fake_audit_denial)

    pool = cast(AsyncConnectionPool, object())
    ctx = cast(RequestContext, object())
    resp = asyncio.run(
        query_platform_audited_page(
            pool,
            ctx,
            tool="audit.read",
            object_id="obj",
            list_tag="audit",
            args={"a": 1},
            limit=10,
            cursor=None,
            fetch_rows=_fetch_rows,
            row_data=_row_data,
            row_object_id=_row_object_id,
        )
    )

    assert resp.object_id == "obj"
    assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
    assert resp.suggested_next_actions == ["audit.read"]
    assert denials == [(pool, ctx, "audit.read", {"a": 1})]
