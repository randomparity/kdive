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
from kdive.mcp.middleware.exposure import NARROWED_TOOLS
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tool_payloads import AllocationRequestPayload, ResourceByKind, ResourceByPool
from kdive.mcp.tools import gateway
from kdive.mcp.tools.lifecycle.allocations import registrar as allocations_registrar
from kdive.mcp.tools.lifecycle.allocations.request import _guard_resource_kind
from kdive.mcp.tools.lifecycle.systems import registrar as systems_registrar
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


# ---------------------------------------------------------------------------
# NARROWED_TOOLS / call-time guard sync (FIX 1, ADR-0269 adversarial review)
# ---------------------------------------------------------------------------

# A fault-inject profile is non-composed on a local-libvirt-only resolver.
# boot_method=direct-kernel + fault-inject: _pair_boot_method_with_provider passes
# (only remote-libvirt requires disk-image).
_FAULT_INJECT_PROFILE: dict = {
    "schema_version": 1,
    "arch": "x86_64",
    "boot_method": "direct-kernel",
    "kernel_source_ref": "linux-test",
    "provider": {"fault-inject": {}},
}

# Arguments to drive each NARROWED_TOOLS member with a non-composed kind.
# Keyed to tool name: a KeyError here when iterating NARROWED_TOOLS means a new
# tool was added to the set without a corresponding entry — add one.
_NON_COMPOSED_ARGS_BY_TOOL: dict[str, dict] = {
    "allocations.request": _NON_COMPOSED_REQUEST_ARGS,
    "systems.define": {
        "allocation_id": "00000000-0000-0000-0000-000000000001",
        "profile": _FAULT_INJECT_PROFILE,
    },
    "systems.provision": {
        "allocation_id": "00000000-0000-0000-0000-000000000001",
        "profile": _FAULT_INJECT_PROFILE,
    },
    "systems.reprovision": {
        "system_id": "00000000-0000-0000-0000-000000000001",
        "profile": _FAULT_INJECT_PROFILE,
    },
}


def _build_full_test_app(resolver: ProviderResolver) -> FastMCP:
    """Minimal FastMCP app with allocations, systems, and gateway tools."""
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = FastMCP(name="test-narrowed-guard")
    allocations_registrar.register(app, pool, resolver=resolver)
    systems_registrar.register(app, pool, resolver=resolver)
    gateway.register(app, resolver=resolver)
    return app


def test_narrowed_tools_exact_membership() -> None:
    """NARROWED_TOOLS must be exactly the four provisioning-choice surfaces (ADR-0269 §4).

    Any addition to NARROWED_TOOLS is a deliberate, reviewed schema-narrowing decision;
    pinning the exact set here makes that intent visible via a test failure.
    """
    assert (
        frozenset(
            {"allocations.request", "systems.define", "systems.provision", "systems.reprovision"}
        )
        == NARROWED_TOOLS
    )


def test_narrowed_tools_sync_with_call_time_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every tool in NARROWED_TOOLS rejects a non-composed kind with configuration_error.

    Iterates NARROWED_TOOLS via ``_NON_COMPOSED_ARGS_BY_TOOL`` so that adding a tool to
    the set without wiring its call-time guard makes this test fail — either via a
    KeyError (missing entry in the args map) or via the tool not returning
    ``configuration_error`` when driven with a non-composed provider kind.

    The systems tools call ``current_context()`` before the guard; patching it in both
    registrar modules is safe because the guard fires before any DB work and the
    patched context is never exercised by a successful guard rejection.
    """
    resolver = _local_libvirt_only_resolver()
    app = _build_full_test_app(resolver)
    monkeypatch.setattr(allocations_registrar, "current_context", _ctx)
    monkeypatch.setattr(systems_registrar, "current_context", _ctx)

    for tool_name in sorted(NARROWED_TOOLS):
        args = _NON_COMPOSED_ARGS_BY_TOOL[tool_name]  # KeyError → add missing entry

        async def _run(name: str = tool_name, a: dict = args) -> ToolResponse:
            result = await app.call_tool(name, a)
            return ToolResponse.model_validate(result.structured_content)

        resp = asyncio.run(_run())
        assert resp.error_category == "configuration_error", (
            f"{tool_name}: expected configuration_error for non-composed kind, "
            f"got {resp.error_category!r}"
        )
        assert "fault-inject" in (resp.detail or ""), (
            f"{tool_name}: expected 'fault-inject' in detail, got {resp.detail!r}"
        )


def test_rejection_envelope_enumerates_composed_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composed kinds survive serialization into the delivered envelope (ADR-0269).

    This is the regression test for the ``"registered"`` → ``"available"`` rename:
    ``safe_error_details`` only preserves lists under keys in ``_ENUMERATION_KEYS``
    (``{"accepted_values", "available"}``).  Emitting under ``"registered"`` silently
    dropped the list; ``"available"`` preserves it so callers can enumerate valid kinds.
    """
    resolver = _local_libvirt_only_resolver()
    app = _build_test_app(resolver)
    monkeypatch.setattr(allocations_registrar, "current_context", _ctx)

    async def _run() -> ToolResponse:
        result = await app.call_tool("allocations.request", _NON_COMPOSED_REQUEST_ARGS)
        return ToolResponse.model_validate(result.structured_content)

    resp = asyncio.run(_run())
    assert resp.error_category == "configuration_error"
    # The envelope's data must carry the composed-kinds list after safe_error_details
    # filtering.  If "available" is not in _ENUMERATION_KEYS, the list is dropped and
    # this assertion fails — catching the serialization drop that ADR-0269 requires.
    data = resp.data or {}
    available = data.get("available")
    assert isinstance(available, list), f"expected 'available' list in envelope data, got: {data!r}"
    assert "local-libvirt" in available
