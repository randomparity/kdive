"""The destructive-operation gate's MCP-side caller, shared by every gated tool (ADR-0028).

`control.force_crash` and `systems.force_unplug_device` both authorize through
``authorize_destructive``: the caller's project role and the System profile's opt-in are the
gate's two independent factors, and a denial is audited and answered ``authorization_denied``
naming every missing factor. The in-project check must precede the call, so the denial audit's
``project`` is always readable (ADR-0028 ordering invariant).
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from psycopg import AsyncConnection

from kdive.db.repositories import ALLOCATIONS
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import JobKind
from kdive.mcp.responses import JsonValue, ToolResponse
from kdive.mcp.tools._common import authz_denied as _authz_denied
from kdive.mcp.tools._common import config_error as _config_error
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.resolver import ProviderResolver
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.gate import DestructiveOp, DestructiveOpDenied, assert_destructive_allowed


async def authorize_destructive(
    conn: AsyncConnection,
    ctx: RequestContext,
    system: System,
    system_uid: UUID,
    op_kind: JobKind,
    *,
    resolver: ProviderResolver,
    tool: str,
    audit_args: Mapping[str, JsonValue] | None = None,
) -> ToolResponse | None:
    """Run the two-check gate; ``None`` when allowed, else the audited denial envelope.

    ``audit_args`` names the operation's target (for example a device id) in the denial audit,
    beside ``system_id`` and ``missing``.
    """
    allocation = await ALLOCATIONS.get(conn, system.allocation_id)
    if allocation is None or allocation.project not in ctx.projects:
        return _config_error(str(system_uid))
    op = DestructiveOp(
        kind=op_kind, profile_opt_in=await _op_opt_in(conn, system, op_kind, resolver)
    )
    try:
        assert_destructive_allowed(ctx, allocation, op)
    except DestructiveOpDenied as denied:
        async with conn.transaction():
            await audit.record(
                conn,
                ctx,
                audit.AuditEvent(
                    tool=tool,
                    object_kind="systems",
                    object_id=system_uid,
                    transition=f"{op_kind.value}:denied",
                    args={
                        "system_id": str(system_uid),
                        **(audit_args or {}),
                        "missing": denied.missing,
                    },
                    project=system.project,
                ),
            )
        return _authz_denied(str(system_uid), denied.missing)
    return None


async def _op_opt_in(
    conn: AsyncConnection, system: System, op_kind: JobKind, resolver: ProviderResolver
) -> bool:
    """Resolve the gate's profile opt-in factor from the System's provisioning profile."""
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    runtime = await resolver.runtime_for_system(conn, system.id)
    return runtime.profile_policy.destructive_opt_in(profile, op_kind)
