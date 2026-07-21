"""Per-connection tool-exposure classification + visibility rule (ADR-0148, #506).

`list_tools` is connection-scoped while project roles are per-project, so a tool is
visible iff the caller could invoke it under *some* grant: the union of project roles
plus the connection's platform roles. Tools with two independent gates (a project role
*or* a platform role) carry an any-of scope set, so the conservative rule never hides a
tool the caller could call through either path.

Classification is a central reviewed map (the `_docmeta.DESTRUCTIVE_TOOLS` idiom), keyed
to each handler's real `require_role` / `require_platform_role` enforcement. An
unclassified tool defaults to public (empty scope set → always visible): a too-permissive
classification only costs catalog size, while a too-restrictive one would hide a usable
tool — the one outcome this filter forbids. The completeness guard
(`tests/mcp/core/test_app.py`) asserts ``CLASSIFIED_TOOLS | PUBLIC_TOOLS`` equals the live
registry so a new tool must be consciously triaged.

This filter is advisory, not a security control: execution-time RBAC remains the boundary
(ADR-0006, ADR-0020, ADR-0043).
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

import kdive.config as config
from kdive.config.core_settings import MCP_TOOL_GATEWAY
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import (
    PlatformRole,
    Role,
    platform_role_satisfies,
    role_satisfies,
)


def gateway_enabled() -> bool:
    """Return True when KDIVE_MCP_TOOL_GATEWAY is set to on/1/true (default off, ADR-0268).

    Single source of truth for the gateway toggle: the exposure middleware reads it to
    decide whether to clip ``list_tools`` to ``CORE_TOOLS``, and ``build_instructions``
    reads it so the advertised instructions match the surface the agent actually sees.
    """
    return (config.get(MCP_TOOL_GATEWAY) or "").strip().lower() in {"on", "1", "true"}


class ExposureScope(StrEnum):
    """One authorization a caller may hold to make a tool worth advertising.

    A tool requires an any-of set of these; a caller satisfying any one sees it. Public
    tools carry the empty set.
    """

    PROJECT_VIEWER = "project_viewer"
    PROJECT_CONTRIBUTOR = "project_contributor"
    PROJECT_OPERATOR = "project_operator"
    PROJECT_ADMIN = "project_admin"
    PLATFORM_OPERATOR = "platform_operator"
    PLATFORM_ADMIN = "platform_admin"
    PLATFORM_AUDITOR = "platform_auditor"


_PROJECT_SCOPE: dict[ExposureScope, Role] = {
    ExposureScope.PROJECT_VIEWER: Role.VIEWER,
    ExposureScope.PROJECT_CONTRIBUTOR: Role.CONTRIBUTOR,
    ExposureScope.PROJECT_OPERATOR: Role.OPERATOR,
    ExposureScope.PROJECT_ADMIN: Role.ADMIN,
}
_PLATFORM_SCOPE: dict[ExposureScope, PlatformRole] = {
    ExposureScope.PLATFORM_OPERATOR: PlatformRole.PLATFORM_OPERATOR,
    ExposureScope.PLATFORM_ADMIN: PlatformRole.PLATFORM_ADMIN,
    ExposureScope.PLATFORM_AUDITOR: PlatformRole.PLATFORM_AUDITOR,
}

_VIEWER = frozenset({ExposureScope.PROJECT_VIEWER})
_CONTRIBUTOR = frozenset({ExposureScope.PROJECT_CONTRIBUTOR})
_OPERATOR = frozenset({ExposureScope.PROJECT_OPERATOR})
_ADMIN = frozenset({ExposureScope.PROJECT_ADMIN})
_PLAT_OP = frozenset({ExposureScope.PLATFORM_OPERATOR})
_PLAT_ADMIN = frozenset({ExposureScope.PLATFORM_ADMIN})
_PLAT_AUDITOR = frozenset({ExposureScope.PLATFORM_AUDITOR})

# Reviewed classification, keyed to each handler's real require_role / require_platform_role
# (the lowest bar that lets the tool do anything; any-of when two independent gates exist).
# A classification must stay <= the handler's real requirement. Public tools are listed in
# PUBLIC_TOOLS, not here. The completeness guard pins CLASSIFIED_TOOLS | PUBLIC_TOOLS to the
# live registry.
_TOOL_SCOPES: dict[str, frozenset[ExposureScope]] = {
    # accounting
    "accounting.usage_project": _VIEWER,
    "accounting.usage_investigation": _VIEWER,
    "accounting.estimate": _VIEWER,
    "accounting.report_granted_set": _VIEWER,
    "accounting.report_all_projects": _PLAT_AUDITOR,
    "accounting.set_budget": _ADMIN,
    "accounting.set_quota": _ADMIN,
    # reports
    "reports.generate_granted_set": _VIEWER,
    "reports.generate_all_projects": _PLAT_AUDITOR,
    # allocations
    "allocations.get": _VIEWER,
    "allocations.list": _VIEWER,
    "allocations.wait": _VIEWER,
    "allocations.request": _CONTRIBUTOR,
    "allocations.release": _CONTRIBUTOR,
    "allocations.renew": _CONTRIBUTOR,
    # artifacts
    "artifacts.get": _VIEWER,
    "artifacts.find": _VIEWER,
    "artifacts.list": _VIEWER,
    "artifacts.fetch_raw": _CONTRIBUTOR,  # raw vmcore/vmlinux egress, ADR-0243
    "artifacts.create_run_upload": _CONTRIBUTOR,
    "artifacts.create_system_upload": _CONTRIBUTOR,  # define-lane leaseholder control (ADR-0326)
    # audit (dual: project admin or platform auditor)
    "audit.query": frozenset({ExposureScope.PROJECT_ADMIN, ExposureScope.PLATFORM_AUDITOR}),
    # control
    "control.power": _CONTRIBUTOR,  # leaseholder lifecycle over a READY transient VM (ADR-0320)
    "control.force_crash": _ADMIN,
    "control.diagnostic_sysrq": _CONTRIBUTOR,  # non-destructive diagnostic capture, ADR-0285
    "control.watch_for_crash": _CONTRIBUTOR,  # non-destructive out-of-band console watch, ADR-0367
    "control.capture_traffic": _CONTRIBUTOR,  # host-side guest pcap capture, ADR-0385
    # debug
    "debug.list_breakpoints": _CONTRIBUTOR,
    "debug.get_session": _VIEWER,
    "debug.list_sessions": _VIEWER,
    "debug.start_session": _CONTRIBUTOR,
    "debug.end_session": _CONTRIBUTOR,
    "debug.continue": _CONTRIBUTOR,
    "debug.interrupt": _CONTRIBUTOR,
    "debug.step": _CONTRIBUTOR,
    "debug.next": _CONTRIBUTOR,
    "debug.step_instruction": _CONTRIBUTOR,
    "debug.finish": _CONTRIBUTOR,
    "debug.set_breakpoint": _CONTRIBUTOR,
    "debug.clear_breakpoint": _CONTRIBUTOR,
    "debug.read_memory": _CONTRIBUTOR,
    "debug.read_registers": _CONTRIBUTOR,
    "debug.resolve_symbol": _CONTRIBUTOR,
    "debug.backtrace": _CONTRIBUTOR,
    "debug.read_frame": _CONTRIBUTOR,
    "debug.disassemble": _CONTRIBUTOR,
    "debug.set_watchpoint": _CONTRIBUTOR,
    "debug.list_watchpoints": _CONTRIBUTOR,
    "debug.clear_watchpoint": _CONTRIBUTOR,
    "debug.list_modules": _CONTRIBUTOR,
    "debug.load_module_symbols": _CONTRIBUTOR,
    # images
    "images.build": _PLAT_OP,
    "images.publish": _PLAT_OP,
    "images.upload": _OPERATOR,
    "images.delete": _OPERATOR,
    "images.extend": _PLAT_ADMIN,
    "images.prune_expired": _PLAT_ADMIN,
    # introspect
    "introspect.from_vmcore": _VIEWER,
    # introspect.run actively drives a live drgn-live session (resolve_debug_session_context →
    # contributor), unlike from_vmcore's offline-core read.
    "introspect.run": _CONTRIBUTOR,
    # introspect.script runs a caller drgn script in the live guest (mutating; same live-debug
    # contributor gate as introspect.run / debug.*; ADR-0240).
    "introspect.script": _CONTRIBUTOR,
    # inventory (platform auditor)
    "inventory.list": _PLAT_AUDITOR,
    "inventory.clear_override": _PLAT_ADMIN,
    # investigations
    "investigations.get": _VIEWER,
    "investigations.list": _VIEWER,
    "investigations.open": _CONTRIBUTOR,
    "investigations.close": _CONTRIBUTOR,
    "investigations.link": _CONTRIBUTOR,
    "investigations.unlink": _CONTRIBUTOR,
    "investigations.set": _CONTRIBUTOR,
    # jobs
    "jobs.get": _VIEWER,
    "jobs.list": _VIEWER,
    "jobs.wait": _VIEWER,
    "jobs.cancel": _CONTRIBUTOR,  # lowest bar: contributor cancels leaseholder-kind jobs
    # (incl. the provision lane, ADR-0326); the handler keeps operator for the destructive kinds
    # ops (platform)
    "ops.diagnostics": _PLAT_OP,
    "ops.export_cost_classes": _PLAT_OP,
    "ops.export_systems_toml": _PLAT_OP,
    "ops.jobs_list": _PLAT_OP,
    "ops.queue_pause": _PLAT_OP,
    "ops.queue_resume": _PLAT_OP,
    "ops.reconcile_now": _PLAT_OP,
    "ops.set_cost_class_coeff": _PLAT_OP,
    "ops.set_host_capacity": _PLAT_OP,
    "ops.tool_trail": _PLAT_AUDITOR,  # cross-tenant per-call trail read (ADR-0304)
    "ops.force_release": _PLAT_ADMIN,
    "ops.force_teardown": _PLAT_ADMIN,
    "ops.reconcile_systems": _PLAT_ADMIN,
    # postmortem
    "postmortem.crash": _CONTRIBUTOR,
    "postmortem.triage": _CONTRIBUTOR,
    # resources (drain is dual: operator or admin)
    "resources.cordon": _PLAT_OP,
    "resources.uncordon": _PLAT_OP,
    "resources.set_status": _PLAT_OP,
    "resources.deregister": _PLAT_ADMIN,
    "resources.renew": _PLAT_ADMIN,
    "resources.register_local_libvirt": _PLAT_ADMIN,
    "resources.register_remote_libvirt": _PLAT_ADMIN,
    "resources.register_fault_inject": _PLAT_ADMIN,
    "resources.drain": frozenset({ExposureScope.PLATFORM_OPERATOR, ExposureScope.PLATFORM_ADMIN}),
    # runs
    "runs.get": _VIEWER,
    "runs.list": _VIEWER,
    "runs.create": _CONTRIBUTOR,
    "runs.bind": _CONTRIBUTOR,
    "runs.cancel": _CONTRIBUTOR,
    "runs.set": _CONTRIBUTOR,
    "runs.complete_build": _CONTRIBUTOR,
    "runs.install": _CONTRIBUTOR,
    "runs.boot": _CONTRIBUTOR,
    # secrets (platform operator)
    "secrets.list": _PLAT_OP,
    # shapes
    "shapes.set": _PLAT_OP,
    "shapes.delete": _PLAT_OP,
    # systems
    "systems.get": _VIEWER,
    "systems.list": _VIEWER,
    "systems.define": _CONTRIBUTOR,  # provision lane is leaseholder control (ADR-0326)
    "systems.provision": _CONTRIBUTOR,
    "systems.provision_defined": _CONTRIBUTOR,
    "systems.reprovision": _CONTRIBUTOR,
    "systems.teardown": _ADMIN,
    "systems.ssh_info": _VIEWER,
    "systems.check_ssh_reachable": _VIEWER,
    "systems.authorize_ssh_key": _CONTRIBUTOR,  # add a key to a VM the caller already sudos
    "systems.snapshot": _CONTRIBUTOR,  # checkpoint a leaseholder's own transient VM (ADR-0378)
    "systems.restore": _CONTRIBUTOR,
    "systems.list_snapshots": _VIEWER,
    "systems.delete_snapshot": _CONTRIBUTOR,
    # vmcore
    "vmcore.list": _VIEWER,
    "vmcore.fetch": _CONTRIBUTOR,
}

#: The default-listed core set when the gateway is on (ADR-0268). Everything else is reachable
#: via tools.search + tools.invoke. CORE_TOOLS must be a subset of the live registry (guard test
#: in tests/mcp/core/test_app.py).
CORE_TOOLS: frozenset[str] = frozenset(
    {
        "tools.search",
        "tools.invoke",
        "session.whoami",
        "runs.create",
        "runs.get",
        "runs.list",
        "allocations.request",
        "allocations.wait",
        "systems.provision",
    }
)

#: Reviewed intentionally-public tools (open reads / onboarding / catalog). Each is callable
#: by any authenticated token (the handler enforces no role, filtering its own results), so
#: hiding it would be wrong. ``CLASSIFIED_TOOLS | PUBLIC_TOOLS`` must equal the live registry.
PUBLIC_TOOLS: frozenset[str] = frozenset(
    {
        "artifacts.expected_uploads",
        "artifacts.feature_config_requirements",
        "fixtures.list",
        "fixtures.validate",
        "images.describe",
        "images.kernel_config",
        "images.list",
        "projects.list",
        "resources.availability",
        "resources.describe",
        "resources.list",
        "session.whoami",
        "shapes.list",
        "systems.profile_examples",
        "tools.invoke",
        "tools.search",
    }
)

#: Union of every gated tool, for the completeness guard.
CLASSIFIED_TOOLS: frozenset[str] = frozenset(_TOOL_SCOPES)


def required_scopes(tool_name: str) -> frozenset[ExposureScope]:
    """The any-of scopes a caller must satisfy to see ``tool_name``; empty = public."""
    return _TOOL_SCOPES.get(tool_name, frozenset())


def scope_satisfied(scope: ExposureScope, ctx: RequestContext) -> bool:
    """Whether ``ctx`` holds the grant ``scope`` names, under any granted project."""
    project_role = _PROJECT_SCOPE.get(scope)
    if project_role is not None:
        return any(role_satisfies(ctx.roles.get(project), project_role) for project in ctx.projects)
    return platform_role_satisfies(ctx.platform_roles, _PLATFORM_SCOPE[scope])


def tool_visible(tool_name: str, ctx: RequestContext) -> bool:
    """Whether ``ctx`` could invoke ``tool_name`` under some grant (public ⇒ always)."""
    scopes = required_scopes(tool_name)
    if not scopes:
        return True
    return any(scope_satisfied(scope, ctx) for scope in scopes)


def visible_tool_names(ctx: RequestContext, names: Iterable[str]) -> set[str]:
    """The subset of ``names`` visible to ``ctx``."""
    return {name for name in names if tool_visible(name, ctx)}


def _project_scope_satisfied(scope: ExposureScope, ctx: RequestContext, project: str) -> bool:
    """Whether ``ctx`` holds ``scope`` *for ``project``* (ADR-0261).

    A project-role scope is satisfied only by the role held on ``project`` itself — not the
    connection-wide maximum :func:`scope_satisfied` uses — so a caller who is operator on another
    project does not thereby satisfy a project scope here. A platform-role scope is not
    project-scoped, so it reuses the connection's platform grants.
    """
    project_role = _PROJECT_SCOPE.get(scope)
    if project_role is not None:
        return role_satisfies(ctx.roles.get(project), project_role)
    return platform_role_satisfies(ctx.platform_roles, _PLATFORM_SCOPE[scope])


def project_tool_visible(tool_name: str, ctx: RequestContext, project: str) -> bool:
    """Whether ``ctx`` could invoke ``tool_name`` *for ``project``* (public ⇒ always, ADR-0261).

    The project-scoped counterpart to :func:`tool_visible`: an allocation belongs to one project,
    so a success-envelope breadcrumb must be filtered against the role held on *that* project, not
    the connection-wide union. Used by :func:`visible_next_actions`.
    """
    scopes = required_scopes(tool_name)
    if not scopes:
        return True
    return any(_project_scope_satisfied(scope, ctx, project) for scope in scopes)


def visible_next_actions(actions: Iterable[str], ctx: RequestContext, project: str) -> list[str]:
    """Drop suggested next-action tool names ``ctx`` cannot invoke for ``project`` (ADR-0261).

    Preserves order and does not deduplicate; an all-filtered or empty input yields ``[]``.
    """
    return [name for name in actions if project_tool_visible(name, ctx, project)]
