"""Call-time guard for non-composed resource kinds (ADR-0269).

Unit tests for ``_guard_resource_kind`` + end-to-end tests confirming the guard fires
through the registered tool closure (both direct ``allocations.request`` and via
``tools.invoke``), so a schema-only bypass cannot circumvent it.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tool_payloads import AllocationRequestPayload, ResourceByKind, ResourceByPool
from kdive.mcp.tools import gateway
from kdive.mcp.tools.lifecycle.allocations import registrar as allocations_registrar
from kdive.mcp.tools.lifecycle.allocations.request import _guard_resource_kind
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.rbac import Role

# ---------------------------------------------------------------------------
# Resolver factories
# ---------------------------------------------------------------------------


def _resolver(*kinds: ResourceKind) -> ProviderResolver:
    """Build a ProviderResolver containing exactly the given kinds (fake runtimes).

    The guard only calls ``registered_kinds()`` — it never invokes the runtime — so
    ``cast(Any, object())`` is a safe placeholder for the runtime value.
    """
    return ProviderResolver({kind: cast(Any, object()) for kind in kinds})


def _ctx() -> RequestContext:
    return RequestContext(
        principal="user-1",
        agent_session="s",
        projects=("proj",),
        roles={"proj": Role.OPERATOR},
    )


def _local_libvirt_only_resolver() -> ProviderResolver:
    """ProviderResolver composed with only local-libvirt (fault-inject absent)."""
    return _resolver(ResourceKind.LOCAL_LIBVIRT)


# ---------------------------------------------------------------------------
# Unit tests: _guard_resource_kind
# ---------------------------------------------------------------------------


def test_guard_rejects_non_composed_kind() -> None:
    payload = AllocationRequestPayload(
        shape="small", resource=ResourceByKind(kind=ResourceKind.FAULT_INJECT)
    )
    with pytest.raises(CategorizedError) as exc:
        _guard_resource_kind(payload, _resolver(ResourceKind.LOCAL_LIBVIRT))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_guard_accepts_composed_kind() -> None:
    payload = AllocationRequestPayload(
        shape="small", resource=ResourceByKind(kind=ResourceKind.LOCAL_LIBVIRT)
    )
    _guard_resource_kind(payload, _resolver(ResourceKind.LOCAL_LIBVIRT))


def test_guard_ignores_non_kind_selectors() -> None:
    # A pool/id selector names no kind, so the guard is a no-op even with NO providers composed
    # (resolution fails closed downstream for an absent resource).
    payload = AllocationRequestPayload(shape="small", resource=ResourceByPool(pool="p"))
    _guard_resource_kind(payload, _resolver())  # no raise


# ---------------------------------------------------------------------------
# End-to-end tests: guard on the shared registered-tool handler path
# ---------------------------------------------------------------------------

_NON_COMPOSED_REQUEST_ARGS: dict[str, Any] = {
    "project": "proj",
    "request": {
        "resource": {"mode": "kind", "kind": "fault-inject"},
        "shape": "small",
    },
}


def _build_test_app(resolver: ProviderResolver) -> FastMCP:
    """Minimal FastMCP app — no auth/middleware — with allocations + gateway tools."""
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = FastMCP(name="test-guard")
    allocations_registrar.register(app, pool, resolver=resolver)
    gateway.register(app, resolver=resolver)
    return app


def test_registered_handler_rejects_non_composed_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard fires in the registered closure, returning a configuration_error envelope.

    This proves the guard is on the handler path, not a schema-only check: the ``request``
    payload is fully valid (fault-inject is a real ResourceKind enum value) so schema
    validation passes, but the handler rejects it because the resolver has no fault-inject.
    """
    resolver = _local_libvirt_only_resolver()
    app = _build_test_app(resolver)
    # current_context is only called after the guard; patching for completeness.
    monkeypatch.setattr(allocations_registrar, "current_context", _ctx)

    async def _run() -> ToolResponse:
        result = await app.call_tool("allocations.request", _NON_COMPOSED_REQUEST_ARGS)
        return ToolResponse.model_validate(result.structured_content)

    resp = asyncio.run(_run())
    assert resp.error_category == "configuration_error"
    # The guard detail names the rejected kind.
    assert "fault-inject" in (resp.detail or "")


def test_gateway_rejects_non_composed_kind_via_tools_invoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard fires through ``tools.invoke`` — the shared ``app.call_tool`` dispatch path.

    ``tools.invoke`` calls ``app.call_tool(name, arguments, run_middleware=True)``, which
    re-enters the same handler closure the direct call uses.  A schema-only guard would be
    invisible here (``tools.invoke`` passes raw ``arguments`` and bypasses schema projection);
    a handler-path guard fires regardless.
    """
    resolver = _local_libvirt_only_resolver()
    app = _build_test_app(resolver)
    monkeypatch.setattr(allocations_registrar, "current_context", _ctx)

    async def _run() -> ToolResponse:
        result = await app.call_tool(
            "tools.invoke",
            {
                "name": "allocations.request",
                "arguments": _NON_COMPOSED_REQUEST_ARGS,
            },
        )
        return ToolResponse.model_validate(result.structured_content)

    resp = asyncio.run(_run())
    assert resp.error_category == "configuration_error"
    assert "fault-inject" in (resp.detail or "")
