"""Per-connection tool-exposure classification + visibility rule (#506, ADR-0148).

`list_tools` is connection-scoped while project roles are per-project, so a tool is
visible iff the caller could invoke it under *some* grant (the union of project roles
plus the connection's platform roles, any-of for dual-gated tools).
"""

from __future__ import annotations

from kdive.mcp.exposure import (
    ExposureScope,
    required_scopes,
    scope_satisfied,
    tool_visible,
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


def test_project_role_union_rank() -> None:
    mixed = _ctx(roles={"a": Role.VIEWER, "b": Role.OPERATOR})
    assert scope_satisfied(ExposureScope.PROJECT_OPERATOR, mixed)  # operator in b
    assert scope_satisfied(ExposureScope.PROJECT_VIEWER, mixed)
    assert not scope_satisfied(ExposureScope.PROJECT_ADMIN, mixed)


def test_viewer_only_does_not_satisfy_operator_or_admin() -> None:
    viewer = _ctx(roles={"a": Role.VIEWER})
    assert scope_satisfied(ExposureScope.PROJECT_VIEWER, viewer)
    assert not scope_satisfied(ExposureScope.PROJECT_OPERATOR, viewer)
    assert not scope_satisfied(ExposureScope.PROJECT_ADMIN, viewer)


def test_platform_admin_implies_auditor_only() -> None:
    admin = _ctx(platform=frozenset({PlatformRole.PLATFORM_ADMIN}))
    assert scope_satisfied(ExposureScope.PLATFORM_AUDITOR, admin)  # admin ⊇ auditor
    assert scope_satisfied(ExposureScope.PLATFORM_ADMIN, admin)
    assert not scope_satisfied(ExposureScope.PLATFORM_OPERATOR, admin)  # not implied (ADR-0043)


def test_public_tool_visible_to_anyone() -> None:
    bare = _ctx()
    assert required_scopes("projects.list") == frozenset()
    assert tool_visible("projects.list", bare)
    assert tool_visible("some.unclassified_tool", bare)  # fail-open default


def test_dual_gated_tool_visible_to_either_grant() -> None:
    # audit.query is project ADMIN *or* platform auditor.
    project_admin = _ctx(roles={"a": Role.ADMIN})
    auditor = _ctx(platform=frozenset({PlatformRole.PLATFORM_AUDITOR}))
    operator = _ctx(roles={"a": Role.OPERATOR})
    assert tool_visible("audit.query", project_admin)
    assert tool_visible("audit.query", auditor)
    assert not tool_visible("audit.query", operator)


def test_drain_visible_to_operator_or_admin() -> None:
    # resources.drain needs platform operator (passive) or platform admin (force_release).
    op = _ctx(platform=frozenset({PlatformRole.PLATFORM_OPERATOR}))
    admin = _ctx(platform=frozenset({PlatformRole.PLATFORM_ADMIN}))
    assert tool_visible("resources.drain", op)
    assert tool_visible("resources.drain", admin)


def test_build_host_list_visible_to_platform_auditor() -> None:
    auditor = _ctx(platform=frozenset({PlatformRole.PLATFORM_AUDITOR}))
    operator = _ctx(platform=frozenset({PlatformRole.PLATFORM_OPERATOR}))

    assert required_scopes("build_hosts.list") == frozenset({ExposureScope.PLATFORM_AUDITOR})
    assert tool_visible("build_hosts.list", auditor)
    assert not tool_visible("build_hosts.list", operator)


def test_build_host_mutations_visible_to_platform_admin_only() -> None:
    admin = _ctx(platform=frozenset({PlatformRole.PLATFORM_ADMIN}))
    operator = _ctx(platform=frozenset({PlatformRole.PLATFORM_OPERATOR}))
    tools = {
        "build_hosts.disable",
        "build_hosts.remove",
        "build_hosts.register_ssh",
        "build_hosts.register_ephemeral_libvirt",
    }

    for tool in tools:
        assert required_scopes(tool) == frozenset({ExposureScope.PLATFORM_ADMIN})
        assert tool_visible(tool, admin)
        assert not tool_visible(tool, operator)


def test_image_retention_visible_to_platform_admin_only() -> None:
    admin = _ctx(platform=frozenset({PlatformRole.PLATFORM_ADMIN}))
    operator = _ctx(platform=frozenset({PlatformRole.PLATFORM_OPERATOR}))

    for tool in {"images.extend", "images.prune_expired"}:
        assert required_scopes(tool) == frozenset({ExposureScope.PLATFORM_ADMIN})
        assert tool_visible(tool, admin)
        assert not tool_visible(tool, operator)


def test_resource_mutations_visible_to_platform_admin_only() -> None:
    admin = _ctx(platform=frozenset({PlatformRole.PLATFORM_ADMIN}))
    operator = _ctx(platform=frozenset({PlatformRole.PLATFORM_OPERATOR}))
    tools = {
        "resources.deregister",
        "resources.renew",
        "resources.register_local_libvirt",
        "resources.register_remote_libvirt",
        "resources.register_fault_inject",
    }

    for tool in tools:
        assert required_scopes(tool) == frozenset({ExposureScope.PLATFORM_ADMIN})
        assert tool_visible(tool, admin)
        assert not tool_visible(tool, operator)


def test_no_grants_sees_only_public_subset() -> None:
    bare = _ctx()
    names = {
        "projects.list",  # public
        "jobs.get",  # project viewer
        "allocations.request",  # project operator
        "control.force_crash",  # project admin
        "ops.reconcile_now",  # platform operator
    }
    assert visible_tool_names(bare, names) == {"projects.list"}


def test_viewer_sees_reads_but_not_mutations() -> None:
    viewer = _ctx(roles={"a": Role.VIEWER})
    names = {"jobs.get", "allocations.request", "control.power", "ops.reconcile_now"}
    assert visible_tool_names(viewer, names) == {"jobs.get"}
