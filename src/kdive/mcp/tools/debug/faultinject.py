"""``faultinject.inspect``: read-only guest fault-injection state for a System (ADR-0825)."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ResponseData, ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import capability_unsupported as _capability_unsupported
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.mcp.tools.lifecycle.systems.ssh_access import recorded_endpoint
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.fault_injection import (
    READ_WORST_CASE_SECONDS,
    REPLY_BYTE_CAP,
    FaultInjectionInspector,
)
from kdive.providers.ports.lifecycle import TransportHandleData
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.system_bootstrap_key import (
    load_system_bootstrap_private_key,
    materialized_private_key,
)

_NOT_READY_DETAIL = "System is not ready; fault-injection state is readable only on a ready System."
_UNPROVISIONED_DETAIL = (
    "This System records no SSH forward, so the guest cannot be read; reprovision a System "
    "created before the forward was rendered."
)
_CAPABILITY = "fault_injection"
_SUPPORTED_PROVIDERS = ["local-libvirt"]


async def inspect_faults(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: str,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> ToolResponse:
    """Read each allowlisted knob's state from a ready local-libvirt System (viewer)."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _invalid_uuid_error("system_id", system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _not_found(system_id)
            require_role(ctx, system.project, Role.VIEWER)
            if system.state is not SystemState.READY:
                return ToolResponse.failure(
                    system_id, ErrorCategory.READINESS_FAILURE, detail=_NOT_READY_DETAIL
                )
            try:
                runtime = (await resolver.binding_for_system(conn, uid)).runtime
                inspector = runtime.fault_injection
                if inspector is None:
                    return _capability_unsupported(
                        system_id,
                        capability=_CAPABILITY,
                        provider=runtime.support.component_sources.provider,
                        supported=_SUPPORTED_PROVIDERS,
                    )
                recorded, _forward = await recorded_endpoint(conn, resolver, system)
                if recorded is None:
                    return ToolResponse.failure(
                        system_id,
                        ErrorCategory.CONFIGURATION_ERROR,
                        detail=_UNPROVISIONED_DETAIL,
                        data={"reason": "ssh_not_provisioned"},
                    )
                private_key = await load_system_bootstrap_private_key(
                    conn, uid, secret_registry=secret_registry
                )
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(system_id, exc)
    # The pooled connection is released before the SSH round trip, which can take seconds.
    host, port = recorded
    handle = TransportHandleData(kind="ssh", host=host, port=port).encode()
    return await _read_state(system_id, inspector, handle, private_key)


async def _read_state(
    system_id: str, inspector: FaultInjectionInspector, handle: str, private_key: str
) -> ToolResponse:
    try:
        with materialized_private_key(private_key) as key_path:
            state = await asyncio.to_thread(
                inspector.read_state, transport_handle=handle, key_path=str(key_path)
            )
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(system_id, exc)
    knobs: ResponseData = {}
    for name, knob in state.items():
        knobs[name] = {"present": knob.present, "armed": knob.armed, **knob.parameters}
    return ToolResponse.success(
        system_id, "ok", suggested_next_actions=["systems.get"], data={"knobs": knobs}
    )


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> None:
    """Register the `faultinject.inspect` tool."""

    @app.tool(
        name="faultinject.inspect",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def faultinject_inspect(
        system_id: Annotated[
            str,
            Field(
                description=(
                    "A ready local-libvirt System whose guest image carries the "
                    "kdive-faultinject helper and whose kernel enables fault injection."
                )
            ),
        ],
    ) -> ToolResponse:
        """Read the kernel fault-injection knobs of a ready local-libvirt System. Requires viewer.

        Reads failslab and fail_page_alloc through the guest's kdive-faultinject helper over the
        System's loopback SSH. Returns data.knobs, keyed by knob name. Each knob has present (bool,
        false when the kernel lacks it), armed (bool), and, when present, the current integers
        probability (percent, 0 to 100), interval, times (-1 is unlimited, 0 is exhausted), space,
        verbose, and for fail_page_alloc min-order. A knob is armed when probability is above 0 and
        times is not 0: it can then fail allocations in every task in the guest, including sshd.

        The helper reply is capped at {byte_cap} bytes per call and is never truncated: an
        oversized or malformed reply is infrastructure_failure and retrying changes nothing. A
        call takes at most about {seconds} seconds. While a knob is armed the guest can fail this
        call itself (transport_failure or infrastructure_failure).

        Errors: not_found (unknown System or project); readiness_failure (System is not ready);
        configuration_error with data.reason capability_unsupported (provider is not
        local-libvirt), ssh_not_provisioned (no SSH forward), debugfs_not_mounted (mount debugfs in
        the guest), fault_injection_unsupported (neither knob exists: boot a kernel with
        CONFIG_FAULT_INJECTION, CONFIG_FAILSLAB and CONFIG_FAIL_PAGE_ALLOC) or helper_not_installed
        (rebuild the guest image with kdive-faultinject staged); transport_failure (ssh to the
        guest failed); infrastructure_failure (the helper failed or replied unexpectedly).
        """
        return await inspect_faults(
            pool,
            current_context(),
            system_id,
            resolver=resolver,
            secret_registry=secret_registry,
        )

    doc = faultinject_inspect.__doc__
    assert doc is not None
    faultinject_inspect.__doc__ = doc.format(
        byte_cap=REPLY_BYTE_CAP, seconds=READ_WORST_CASE_SECONDS
    )
