"""Behavior tests for the read-only ``session.whoami`` identity probe (ADR-0232, #752)."""

from __future__ import annotations

from kdive.mcp.tools.catalog.session import whoami
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, Role


def _ctx(
    *,
    principal: str = "agent-1",
    agent_session: str | None = "sess-9",
    projects: tuple[str, ...] = (),
    roles: dict[str, Role] | None = None,
    platform_roles: frozenset[PlatformRole] = frozenset(),
    client_id: str | None = None,
) -> RequestContext:
    return RequestContext(
        principal=principal,
        agent_session=agent_session,
        projects=projects,
        roles=roles or {},
        platform_roles=platform_roles,
        client_id=client_id,
    )


def test_whoami_projects_the_full_claim_set() -> None:
    ctx = _ctx(
        principal="agent-7",
        projects=("proj-b", "proj-a", "proj-c"),  # proj-c is role-less
        roles={"proj-a": Role.ADMIN, "proj-b": Role.VIEWER},
        platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR, PlatformRole.PLATFORM_AUDITOR}),
        client_id="cli-xyz",
    )

    response = whoami(ctx)

    assert response.object_id == "agent-7"
    assert response.status == "ok"
    assert response.suggested_next_actions == ["projects.list"]
    data = response.data
    assert data["principal"] == "agent-7"
    assert data["client_id"] == "cli-xyz"
    # Sorted, de-duplicated union of role-bearing and role-less memberships.
    assert data["projects"] == ["proj-a", "proj-b", "proj-c"]
    # Only role-bearing projects; the role-less proj-c is absent from roles.
    assert data["roles"] == {"proj-a": "admin", "proj-b": "viewer"}
    assert data["platform_roles"] == ["platform_auditor", "platform_operator"]


def test_whoami_deduplicates_projects() -> None:
    ctx = _ctx(projects=("proj-a", "proj-a", "proj-b"), roles={"proj-a": Role.OPERATOR})

    data = whoami(ctx).data

    assert data["projects"] == ["proj-a", "proj-b"]
    assert data["roles"] == {"proj-a": "operator"}


def test_whoami_empty_context_keeps_every_key_present() -> None:
    ctx = _ctx(principal="agent-bare", agent_session=None)

    response = whoami(ctx)
    data = response.data

    assert response.object_id == "agent-bare"
    assert data["principal"] == "agent-bare"
    assert data["client_id"] is None
    assert data["projects"] == []
    assert data["roles"] == {}
    assert data["platform_roles"] == []


def test_whoami_does_not_leak_agent_session() -> None:
    ctx = _ctx(agent_session="secret-correlator", projects=("proj-a",))

    data = whoami(ctx).data

    assert "agent_session" not in data
    assert "secret-correlator" not in data.values()
