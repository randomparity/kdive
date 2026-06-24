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
    *,
    roles: dict[str, Role] | None = None,
    projects: tuple[str, ...] | None = None,
    platform: frozenset[PlatformRole] = frozenset(),
) -> RequestContext:
    roles = roles or {}
    return RequestContext(
        principal="p",
        agent_session=None,
        projects=tuple(roles) if projects is None else projects,
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


def test_project_roles_on_ungranted_projects_do_not_expose_project_tools() -> None:
    ungranted = _ctx(roles={"a": Role.ADMIN}, projects=())
    other_project = _ctx(roles={"a": Role.ADMIN}, projects=("b",))

    assert not scope_satisfied(ExposureScope.PROJECT_VIEWER, ungranted)
    assert not tool_visible("runs.build", ungranted)
    assert visible_tool_names(ungranted, {"projects.list", "runs.build"}) == {"projects.list"}
    assert not scope_satisfied(ExposureScope.PROJECT_ADMIN, other_project)


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


def test_session_whoami_is_public_and_admits_viewer_and_role_less() -> None:
    # ADR-0232 / #752 AC#3: the identity probe is ungated, so a viewer-only or even a
    # role-less authenticated caller is admitted (sees and may call it).
    viewer_only = _ctx(roles={"a": Role.VIEWER})
    role_less = _ctx(projects=("a",))  # member of "a" with no role
    assert required_scopes("session.whoami") == frozenset()
    assert tool_visible("session.whoami", viewer_only)
    assert tool_visible("session.whoami", role_less)
    assert tool_visible("session.whoami", _ctx())  # token-only, no membership


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


def test_private_image_mutations_visible_to_project_operator_only() -> None:
    project_operator = _ctx(roles={"project-a": Role.OPERATOR})
    platform_operator = _ctx(platform=frozenset({PlatformRole.PLATFORM_OPERATOR}))
    ungranted_operator = _ctx(roles={"project-a": Role.OPERATOR}, projects=())

    for tool in {"images.upload", "images.delete"}:
        assert required_scopes(tool) == frozenset({ExposureScope.PROJECT_OPERATOR})
        assert tool_visible(tool, project_operator)
        assert not tool_visible(tool, platform_operator)
        assert not tool_visible(tool, ungranted_operator)


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
