"""``projects.list`` (whoami) handler tests — a pure projection of RequestContext (#427).

The handler is a side-effect-free projection of the request context (no DB, no pool), so
each test builds a ``RequestContext`` and calls ``whoami`` directly. Coverage maps to the
ADR-0117 acceptance criteria: role-bearing grant, role-less membership surfaced, the
always-present top-level ``principal``/``platform_roles`` keys (empty list for a
project-only token), deterministic ordering, and duplicate-grant dedup.
"""

from __future__ import annotations

from typing import cast

from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.identity.projects import whoami
from kdive.security.authz.rbac import PlatformRole, Role
from tests.mcp.json_data import data_sequence, data_str


def _ctx(
    *,
    principal: str = "user-1",
    projects: tuple[str, ...] = (),
    roles: dict[str, Role] | None = None,
    platform_roles: frozenset[PlatformRole] = frozenset(),
) -> RequestContext:
    return RequestContext(
        principal=principal,
        agent_session="sess-1",
        projects=projects,
        roles=roles or {},
        platform_roles=platform_roles,
    )


def _items(resp: ToolResponse) -> list[dict[str, object]]:
    return [cast(dict[str, object], item.data) for item in resp.items]


def test_role_bearing_grant_names_project_role_and_platform_roles() -> None:
    ctx = _ctx(
        principal="kdive-demo",
        projects=("demo",),
        roles={"demo": Role.ADMIN},
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
    )
    resp = whoami(ctx)
    assert resp.status == "ok"
    assert resp.error_category is None
    assert _items(resp) == [{"project": "demo", "role": "admin"}]
    assert data_str(resp, "principal") == "kdive-demo"
    assert list(data_sequence(resp, "platform_roles")) == ["platform_admin"]
    assert resp.data["count"] == 1
    assert resp.suggested_next_actions == ["accounting.report_granted_set"]


def test_role_less_membership_is_surfaced_with_empty_role() -> None:
    # The discovery gap #426 deferred here: a member with no role is named, role "".
    ctx = _ctx(projects=("x",), roles={})
    resp = whoami(ctx)
    assert resp.status == "ok"
    assert _items(resp) == [{"project": "x", "role": ""}]


def test_each_granted_project_item_has_ok_status() -> None:
    ctx = _ctx(
        projects=("a", "b"),
        roles={"a": Role.VIEWER, "b": Role.OPERATOR},
    )
    resp = whoami(ctx)
    assert [item.status for item in resp.items] == ["ok", "ok"]


def test_platform_only_token_has_no_items_but_reports_platform_roles() -> None:
    ctx = _ctx(
        projects=(),
        platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}),
    )
    resp = whoami(ctx)
    assert resp.status == "ok"
    assert _items(resp) == []
    assert resp.data["count"] == 0
    assert list(data_sequence(resp, "platform_roles")) == ["platform_auditor"]
    assert data_str(resp, "principal") == "user-1"


def test_project_only_token_reports_empty_platform_roles_list() -> None:
    # platform_roles is always present as a list — [] (not omitted/None) — so a client
    # can read it unconditionally.
    ctx = _ctx(projects=("demo",), roles={"demo": Role.VIEWER})
    resp = whoami(ctx)
    assert resp.status == "ok"
    platform_roles = data_sequence(resp, "platform_roles")
    assert list(platform_roles) == []
    assert isinstance(platform_roles, list)


def test_items_are_sorted_and_duplicates_collapse() -> None:
    # ctx.projects is not deduplicated upstream (#426); the whoami names each once, sorted.
    ctx = _ctx(
        projects=("c", "a", "a", "b"),
        roles={"a": Role.VIEWER, "b": Role.OPERATOR, "c": Role.ADMIN},
    )
    resp = whoami(ctx)
    assert [item["project"] for item in _items(resp)] == ["a", "b", "c"]
    assert resp.data["count"] == 3


def test_platform_roles_serializes_as_a_json_list() -> None:
    # A list-valued data field is JSON-safe (fixtures.list precedent) and survives the
    # envelope's structured serialization as an actual array.
    ctx = _ctx(
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN, PlatformRole.PLATFORM_AUDITOR}),
    )
    resp = whoami(ctx)
    dumped = resp.model_dump(mode="json")
    assert dumped["data"]["platform_roles"] == ["platform_admin", "platform_auditor"]
