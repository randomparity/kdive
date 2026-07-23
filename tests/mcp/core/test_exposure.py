"""Per-connection tool-exposure classification + visibility rule (#506, ADR-0148).

`list_tools` is connection-scoped while project roles are per-project, so a tool is
visible iff the caller could invoke it under *some* grant (the union of project roles
plus the connection's platform roles, any-of for dual-gated tools).
"""

from __future__ import annotations

import pytest

from kdive.mcp.exposure import (
    ExposureScope,
    project_tool_visible,
    required_scopes,
    scope_satisfied,
    tool_visible,
    visible_next_actions,
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
    assert not tool_visible("runs.complete_build", ungranted)
    assert visible_tool_names(ungranted, {"projects.list", "runs.complete_build"}) == {
        "projects.list"
    }
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
        "allocations.request",  # project contributor
        "control.force_crash",  # project admin
        "ops.reconcile_now",  # platform operator
    }
    assert visible_tool_names(bare, names) == {"projects.list"}


def test_viewer_sees_reads_but_not_mutations() -> None:
    viewer = _ctx(roles={"a": Role.VIEWER})
    names = {"jobs.get", "allocations.request", "control.power", "ops.reconcile_now"}
    assert visible_tool_names(viewer, names) == {"jobs.get"}


def test_contributor_scope_rank() -> None:
    # PROJECT_CONTRIBUTOR sits between viewer and operator (ADR-0234): contributor and up
    # satisfy it; viewer does not.
    contributor = _ctx(roles={"a": Role.CONTRIBUTOR})
    assert scope_satisfied(ExposureScope.PROJECT_CONTRIBUTOR, contributor)
    assert scope_satisfied(ExposureScope.PROJECT_VIEWER, contributor)
    assert not scope_satisfied(ExposureScope.PROJECT_OPERATOR, contributor)
    assert not scope_satisfied(ExposureScope.PROJECT_ADMIN, contributor)
    assert scope_satisfied(ExposureScope.PROJECT_CONTRIBUTOR, _ctx(roles={"a": Role.OPERATOR}))
    assert scope_satisfied(ExposureScope.PROJECT_CONTRIBUTOR, _ctx(roles={"a": Role.ADMIN}))
    assert not scope_satisfied(ExposureScope.PROJECT_CONTRIBUTOR, _ctx(roles={"a": Role.VIEWER}))


#: The external build-debug loop a contributor must be able to see and drive (ADR-0234).
_CONTRIBUTOR_LOOP = frozenset(
    {
        "runs.create",
        "runs.bind",
        "runs.complete_build",
        "runs.install",
        "runs.boot",
        "runs.cancel",
        "artifacts.create_run_upload",
        "debug.start_session",
        "debug.read_memory",
        "debug.list_breakpoints",  # shares the engine-op runtime gate → contributor, not viewer
        "postmortem.crash",
        "postmortem.triage",
        "vmcore.fetch",
        "allocations.request",
        "investigations.open",
        "control.power",  # leaseholder power lifecycle over a READY transient VM (ADR-0320)
        # the provision lane instantiates a System on the slot the contributor holds (ADR-0326)
        "systems.define",
        "systems.provision",
        "systems.provision_defined",
        "systems.reprovision",
        "artifacts.create_system_upload",
    }
)

#: What must remain above contributor: operator-only and admin-only project tools.
_ABOVE_CONTRIBUTOR = frozenset(
    {
        "images.upload",  # mutates the shared cross-tenant image catalog (operator)
        "systems.teardown",  # admin
        "control.force_crash",  # admin
    }
)


def test_contributor_sees_the_full_loop_but_not_operator_tools() -> None:
    contributor = _ctx(roles={"a": Role.CONTRIBUTOR})
    assert visible_tool_names(contributor, _CONTRIBUTOR_LOOP) == _CONTRIBUTOR_LOOP
    assert visible_tool_names(contributor, _ABOVE_CONTRIBUTOR) == frozenset()


def test_viewer_sees_none_of_the_loop() -> None:
    viewer = _ctx(roles={"a": Role.VIEWER})
    assert visible_tool_names(viewer, _CONTRIBUTOR_LOOP) == frozenset()


def test_operator_still_sees_the_whole_loop_and_above() -> None:
    # The rank is a superset: re-gating to contributor never removes an operator's view.
    operator = _ctx(roles={"a": Role.OPERATOR})
    assert visible_tool_names(operator, _CONTRIBUTOR_LOOP) == _CONTRIBUTOR_LOOP
    assert visible_tool_names(operator, _ABOVE_CONTRIBUTOR) == _ABOVE_CONTRIBUTOR - {
        "systems.teardown",
        "control.force_crash",
    }


def test_both_upload_kinds_are_contributor() -> None:
    # The shared upload seam is contributor for both owner kinds: run-upload feeds the build lane,
    # system-upload feeds the leaseholder's define→provision lane (ADR-0326).
    assert required_scopes("artifacts.create_run_upload") == frozenset(
        {ExposureScope.PROJECT_CONTRIBUTOR}
    )
    assert required_scopes("artifacts.create_system_upload") == frozenset(
        {ExposureScope.PROJECT_CONTRIBUTOR}
    )


def test_leaseholder_control_tools_classified_contributor() -> None:
    # Leaseholder-control sweep (#1080): jobs.cancel and systems.authorize_ssh_key each act only
    # on the caller's own already-granted transient resource and are weaker than powers a
    # contributor already holds (runs.cancel is already contributor; authorize_ssh_key adds a key
    # to a VM the caller already sudos). Both classify at contributor exactly, so a contributor
    # discovers them and a viewer does not.
    contributor = _ctx(roles={"a": Role.CONTRIBUTOR})
    viewer = _ctx(roles={"a": Role.VIEWER})
    for tool in ("jobs.cancel", "systems.authorize_ssh_key"):
        assert required_scopes(tool) == frozenset({ExposureScope.PROJECT_CONTRIBUTOR}), tool
        assert tool_visible(tool, contributor), tool
        assert not tool_visible(tool, viewer), tool


#: Every tool that operates on an *existing* live DebugSession routes through the single
#: ``resolve_debug_session_context`` runtime gate (contributor, ADR-0234). The exposure scope is
#: hand-maintained and drifted to VIEWER twice (debug.list_breakpoints, introspect.run), so this
#: list is the invariant: a tool in this set MUST classify as contributor, or a viewer is shown a
#: tool the shared gate will deny. Keep it in sync with the helper's call sites
#: (`rg resolve_debug_session_context src/kdive`).
_LIVE_SESSION_FAMILY = frozenset(
    {
        "debug.end_session",
        "debug.continue",
        "debug.interrupt",
        "debug.set_breakpoint",
        "debug.clear_breakpoint",
        "debug.list_breakpoints",
        "debug.read_memory",
        "debug.read_registers",
        "introspect.run",
    }
)


def test_live_session_family_classified_contributor_not_viewer() -> None:
    # Guard against the exposure/runtime drift that hid behind list_breakpoints and introspect.run:
    # each tool sharing the contributor live-session gate must advertise at contributor exactly.
    for tool in _LIVE_SESSION_FAMILY:
        assert required_scopes(tool) == frozenset({ExposureScope.PROJECT_CONTRIBUTOR}), tool
    # And the offline-core read stays viewer (it does NOT touch a live session).
    assert required_scopes("introspect.from_vmcore") == frozenset({ExposureScope.PROJECT_VIEWER})


def test_viewer_sees_no_live_session_tool() -> None:
    viewer = _ctx(roles={"a": Role.VIEWER})
    assert visible_tool_names(viewer, _LIVE_SESSION_FAMILY) == frozenset()


# --- project-scoped visibility (#862, ADR-0261) -------------------------------------------


def test_project_tool_visible_honours_role_on_the_named_project() -> None:
    # images.upload is project-operator: only an operator+ on the named project sees it. (The
    # provision lane moved to contributor, ADR-0326, so an operator-only tool is used here.)
    contributor = _ctx(roles={"a": Role.CONTRIBUTOR})
    operator = _ctx(roles={"a": Role.OPERATOR})
    assert not project_tool_visible("images.upload", contributor, "a")
    assert project_tool_visible("images.upload", operator, "a")
    # systems.provision is now contributor-visible on the named project.
    assert project_tool_visible("systems.provision", contributor, "a")


def test_project_tool_visible_is_per_project_not_connection_union() -> None:
    # Operator on b, contributor on a: an operator-only tool (images.upload) must NOT be
    # advertised on a, even though the connection-scoped tool_visible would admit it (#862 bug).
    mixed = _ctx(roles={"a": Role.CONTRIBUTOR, "b": Role.OPERATOR})
    assert tool_visible("images.upload", mixed)  # connection union admits it
    assert not project_tool_visible("images.upload", mixed, "a")
    assert project_tool_visible("images.upload", mixed, "b")


def test_project_tool_visible_member_without_role_sees_only_public() -> None:
    role_less = _ctx(projects=("a",))  # member of a, no role
    assert not project_tool_visible("allocations.get", role_less, "a")  # viewer-gated
    assert project_tool_visible("projects.list", role_less, "a")  # public


def test_project_tool_visible_platform_scope_uses_connection_grant() -> None:
    # A platform-gated tool is not project-scoped; the platform grant decides regardless of project.
    auditor = _ctx(platform=frozenset({PlatformRole.PLATFORM_AUDITOR}))
    assert project_tool_visible("ops.tool_trail", auditor, "a")
    assert not project_tool_visible("ops.tool_trail", _ctx(roles={"a": Role.ADMIN}), "a")


def test_visible_next_actions_filters_preserves_order_no_dedup() -> None:
    contributor = _ctx(roles={"a": Role.CONTRIBUTOR})
    # images.upload stays operator-only; systems.provision is now contributor (ADR-0326).
    actions = ["allocations.get", "images.upload", "systems.provision", "allocations.release"]
    assert visible_next_actions(actions, contributor, "a") == [
        "allocations.get",
        "systems.provision",
        "allocations.release",
    ]
    viewer = _ctx(roles={"a": Role.VIEWER})
    assert visible_next_actions(actions, viewer, "a") == ["allocations.get"]
    operator = _ctx(roles={"a": Role.OPERATOR})
    assert visible_next_actions(actions, operator, "a") == actions
    assert visible_next_actions([], contributor, "a") == []


def test_visible_next_actions_raises_on_unregistered_tool() -> None:
    # An action naming a tool that is not in the live registry is a navigation dead end: without
    # this guard required_scopes() fail-opens to empty scopes, so the unknown name is treated as
    # public and silently kept. The filter must instead surface the drift by raising (#1444).
    contributor = _ctx(roles={"a": Role.CONTRIBUTOR})
    with pytest.raises(ValueError, match="allocations.does_not_exist"):
        visible_next_actions(["allocations.get", "allocations.does_not_exist"], contributor, "a")


def test_visible_next_actions_still_filters_registered_but_invisible() -> None:
    # A registered tool the caller's role cannot invoke on the project still filters cleanly — the
    # unregistered-name guard must not turn a legitimate role-based filter into an error.
    viewer = _ctx(roles={"a": Role.VIEWER})
    # images.upload is registered (project-operator) but a viewer cannot invoke it.
    assert visible_next_actions(["allocations.get", "images.upload"], viewer, "a") == [
        "allocations.get"
    ]
