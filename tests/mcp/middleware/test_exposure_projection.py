"""Unit and integration tests for list-time schema projection (ADR-0269, Task 4)."""

from __future__ import annotations

import asyncio
import logging

from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

import kdive.mcp.middleware.exposure as exposure_mod
from kdive.domain.catalog.resources import ResourceKind
from kdive.mcp.app import build_app
from kdive.mcp.middleware.exposure import (
    NARROWED_TOOLS,
    ToolExposureMiddleware,
    project_listed_tool,
)
from kdive.mcp.schema_advertising import registered_tools
from kdive.mcp.tool_payloads import AllocationRequestPayload
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.assembly.composition import ProviderComposition
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair


class _FakeTool:
    def __init__(self, name: str, parameters: dict) -> None:
        self.name = name
        self.parameters = parameters

    def model_copy(self, *, update: dict) -> _FakeTool:
        return _FakeTool(self.name, update["parameters"])


# ---------------------------------------------------------------------------
# NARROWED_TOOLS membership
# ---------------------------------------------------------------------------


def test_narrowed_tools_membership() -> None:
    assert "systems.define" in NARROWED_TOOLS
    assert "systems.provision" in NARROWED_TOOLS
    assert "systems.reprovision" in NARROWED_TOOLS
    assert "allocations.request" in NARROWED_TOOLS
    assert "resources.list" not in NARROWED_TOOLS


# ---------------------------------------------------------------------------
# project_listed_tool unit tests
# ---------------------------------------------------------------------------


def test_allocation_tool_kind_enum_is_projected() -> None:
    # allocations.request narrows via the $defs.ResourceKind enum.
    tool = _FakeTool("allocations.request", AllocationRequestPayload.model_json_schema())
    out = project_listed_tool(tool, frozenset({ResourceKind.LOCAL_LIBVIRT}))  # ty: ignore[invalid-argument-type]
    assert out.parameters["$defs"]["ResourceKind"]["enum"] == ["local-libvirt"]


def test_systems_tool_section_props_are_projected() -> None:
    # systems.define narrows via $defs.ProviderSection.properties (no ResourceKind enum here).
    tool = _FakeTool("systems.define", ProvisioningProfile.model_json_schema())
    out = project_listed_tool(tool, frozenset({ResourceKind.LOCAL_LIBVIRT}))  # ty: ignore[invalid-argument-type]
    kept = set(out.parameters["$defs"]["ProviderSection"]["properties"])
    assert kept == {"local-libvirt"}


def test_systems_reprovision_section_props_are_projected() -> None:
    # systems.reprovision accepts a ProvisioningProfile and narrows the same ProviderSection union.
    tool = _FakeTool("systems.reprovision", ProvisioningProfile.model_json_schema())
    out = project_listed_tool(tool, frozenset({ResourceKind.LOCAL_LIBVIRT}))  # ty: ignore[invalid-argument-type]
    kept = set(out.parameters["$defs"]["ProviderSection"]["properties"])
    assert kept == {"local-libvirt"}


def test_unaffected_tool_is_returned_unchanged() -> None:
    tool = _FakeTool("resources.list", ProvisioningProfile.model_json_schema())
    out = project_listed_tool(tool, frozenset({ResourceKind.LOCAL_LIBVIRT}))  # ty: ignore[invalid-argument-type]
    assert out is tool


# ---------------------------------------------------------------------------
# Live-app integration: real FastMCP-published schema is narrowable
# ---------------------------------------------------------------------------


def _tool(app, name: str):  # type: ignore[no-untyped-def]
    return next(t for t in registered_tools(app) if t.name == name)


def _build_app() -> object:
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    return build_app(pool, verifier=verifier, secret_registry=SecretRegistry())


def test_real_published_schema_narrows_for_local_only() -> None:
    """The real FastMCP-published schemas for NARROWED_TOOLS carry the $defs the helper targets."""
    app = _build_app()
    kinds = frozenset({ResourceKind.LOCAL_LIBVIRT})
    alloc = project_listed_tool(_tool(app, "allocations.request"), kinds)
    assert alloc.parameters["$defs"]["ResourceKind"]["enum"] == ["local-libvirt"]
    define = project_listed_tool(_tool(app, "systems.define"), kinds)
    assert set(define.parameters["$defs"]["ProviderSection"]["properties"]) == {"local-libvirt"}


def test_resources_list_schema_permissive_on_local_only() -> None:
    """resources.list is NOT in NARROWED_TOOLS; its kind enum must stay full on any deployment.

    ADR-0269 narrows agent-facing provider enumeration only for write/request surfaces.
    The read/query surface (resources.list) enumerates every ResourceKind regardless of
    which providers are composed, so a local-only deployment can still filter by
    'remote-libvirt' without a schema error. Pin that the full enum is preserved after
    projection with local-only kinds.
    """
    app = _build_app()
    rl_tool = _tool(app, "resources.list")
    kinds = frozenset({ResourceKind.LOCAL_LIBVIRT})
    projected = project_listed_tool(rl_tool, kinds)
    # resources.list is not narrowed — projection must return the same object unchanged.
    assert projected is rl_tool
    # And the published schema still carries the full three-value ResourceKind enum.
    all_kind_values = sorted(k.value for k in ResourceKind)
    published_enum = sorted(rl_tool.parameters["$defs"]["ResourceKind"]["enum"])
    assert published_enum == all_kind_values


def test_on_list_tools_projects_visible_tools(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The middleware's on_list_tools applies projection to narrowed tools in a live app."""
    app = _build_app()

    # Grab the real allocations.request tool from the registry
    alloc_tool = _tool(app, "allocations.request")
    tools = [alloc_tool]

    # Build a local-only resolver so projection narrows to local-libvirt
    composition = ProviderComposition(secret_registry=SecretRegistry())
    resolver = composition.build_provider_resolver()

    # Monkeypatch so the RBAC filter passes everything through
    monkeypatch.setattr(exposure_mod, "request_context", lambda: object())
    monkeypatch.setattr(exposure_mod, "visible_tool_names", lambda _ctx, names: set(names))

    mw = ToolExposureMiddleware(resolver)

    async def call_next(_ctx: object) -> list:
        return tools

    result = asyncio.run(mw.on_list_tools(object(), call_next))
    projected = next(t for t in result if t.name == "allocations.request")
    # The schema should be narrowed to whatever registered_kinds() contains.
    kinds = resolver.registered_kinds()
    expected = [k.value for k in ResourceKind if k in kinds]
    assert projected.parameters["$defs"]["ResourceKind"]["enum"] == expected


def test_resolver_failure_fails_open_and_counts(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    """A resolver.registered_kinds() error returns RBAC-visible tools unprojected (ADR-0269).

    Stage 2 is isolated from Stage 1: a registered_kinds() failure increments
    _PROJECTION_FAILURES (not _EXPOSURE_FAILOPEN), returns the RBAC-filtered visible
    tools unprojected, and still excludes non-visible tools (RBAC is preserved).
    """
    alloc_tool = _FakeTool("allocations.request", {"type": "object"})
    hidden_tool = _FakeTool("admin.secret", {"type": "object"})
    tools = [alloc_tool, hidden_tool]

    class _ExplodingResolver:
        def registered_kinds(self) -> frozenset:
            raise RuntimeError("injected resolver failure")

    monkeypatch.setattr(exposure_mod, "request_context", lambda: object())
    # admin.secret is not visible to this connection — RBAC must hold even on resolver failure.
    monkeypatch.setattr(
        exposure_mod,
        "visible_tool_names",
        lambda _ctx, names: {n for n in names if n != "admin.secret"},
    )

    counter_calls: list[int] = []
    monkeypatch.setattr(
        exposure_mod._PROJECTION_FAILURES, "add", lambda amount: counter_calls.append(amount)
    )
    exposure_counter_calls: list[int] = []
    monkeypatch.setattr(
        exposure_mod._EXPOSURE_FAILOPEN, "add", lambda amount: exposure_counter_calls.append(amount)
    )

    mw = ToolExposureMiddleware(_ExplodingResolver())  # ty: ignore[invalid-argument-type]

    async def call_next(_ctx: object) -> list:
        return tools

    with caplog.at_level(logging.WARNING, logger="kdive.mcp.middleware.exposure"):
        result = asyncio.run(mw.on_list_tools(object(), call_next))

    result_names = {t.name for t in result}

    # RBAC preserved: hidden tool excluded even though resolver failed.
    assert "admin.secret" not in result_names

    # Visible tool returned unprojected (resolver failure → kinds=None sentinel).
    assert "allocations.request" in result_names
    alloc_in_result = next(t for t in result if t.name == "allocations.request")
    assert alloc_in_result is alloc_tool  # original, not a projected copy

    # _PROJECTION_FAILURES fired once; _EXPOSURE_FAILOPEN not fired.
    assert counter_calls == [1]
    assert exposure_counter_calls == []
    assert "registered_kinds() failed" in caplog.text


def test_exposure_failopen_increments_exposure_counter(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    """A stage-1 (RBAC/context) failure fires _EXPOSURE_FAILOPEN and returns the full catalog.

    ADR-0269 Stage 1: a non-AuthError from visible_tool_names (or request_context) fires
    _EXPOSURE_FAILOPEN — not _PROJECTION_FAILURES — and returns the full unfiltered catalog.
    The two counters are distinct so operators can distinguish auth plumbing breaks from
    schema-projection failures.
    """
    alloc_tool = _FakeTool("allocations.request", {"type": "object"})
    hidden_tool = _FakeTool("admin.secret", {"type": "object"})
    tools = [alloc_tool, hidden_tool]

    composition = ProviderComposition(secret_registry=SecretRegistry())
    resolver = composition.build_provider_resolver()

    # Stage-1 failure: visible_tool_names raises, simulating RBAC/context plumbing break.
    def _raise_visible(_ctx: object, _names: object) -> set:
        raise RuntimeError("injected RBAC failure")

    monkeypatch.setattr(exposure_mod, "request_context", lambda: object())
    monkeypatch.setattr(exposure_mod, "visible_tool_names", _raise_visible)

    exposure_counter_calls: list[int] = []
    monkeypatch.setattr(
        exposure_mod._EXPOSURE_FAILOPEN, "add", lambda amount: exposure_counter_calls.append(amount)
    )
    projection_counter_calls: list[int] = []
    monkeypatch.setattr(
        exposure_mod._PROJECTION_FAILURES,
        "add",
        lambda amount: projection_counter_calls.append(amount),
    )

    mw = ToolExposureMiddleware(resolver)

    async def call_next(_ctx: object) -> list:
        return tools

    with caplog.at_level(logging.WARNING, logger="kdive.mcp.middleware.exposure"):
        result = asyncio.run(mw.on_list_tools(object(), call_next))

    # Full catalog returned (fail-open; RBAC filter could not run).
    assert len(result) == len(tools)

    # _EXPOSURE_FAILOPEN fired once; _PROJECTION_FAILURES not fired.
    assert exposure_counter_calls == [1]
    assert projection_counter_calls == []
    assert "tool-exposure filter failed" in caplog.text


def test_projection_failure_fails_open_and_counts(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    """A projection error advertises the original tool and fires the failure counter (ADR-0269 §5).

    The middleware must never drop a tool from the catalog due to a projection bug.
    Instead it must revert to the full schema, increment the OTLP counter, and log a
    warning so the silent revert is observable.
    """
    alloc_tool = _FakeTool("allocations.request", {"type": "object"})
    other_tool = _FakeTool("resources.list", {"type": "object"})
    tools = [alloc_tool, other_tool]

    composition = ProviderComposition(secret_registry=SecretRegistry())
    resolver = composition.build_provider_resolver()

    # RBAC: pass everything through.
    monkeypatch.setattr(exposure_mod, "request_context", lambda: object())
    monkeypatch.setattr(exposure_mod, "visible_tool_names", lambda _ctx, names: set(names))

    # Inject a projection failure only for the narrowed tool.
    def _failing_project(tool: object, kinds: object) -> object:
        if getattr(tool, "name", None) == "allocations.request":
            raise RuntimeError("injected projection failure")
        return tool

    monkeypatch.setattr(exposure_mod, "project_listed_tool", _failing_project)

    # Capture counter increments.
    counter_calls: list[int] = []
    monkeypatch.setattr(
        exposure_mod._PROJECTION_FAILURES, "add", lambda amount: counter_calls.append(amount)
    )

    mw = ToolExposureMiddleware(resolver)

    async def call_next(_ctx: object) -> list:
        return tools

    with caplog.at_level(logging.WARNING, logger="kdive.mcp.middleware.exposure"):
        result = asyncio.run(mw.on_list_tools(object(), call_next))

    # Fail-open: narrowed tool is still in the catalog as the original unprojected object.
    result_names = {t.name for t in result}
    assert "allocations.request" in result_names
    alloc_in_result = next(t for t in result if t.name == "allocations.request")
    assert alloc_in_result is alloc_tool  # original, not a projected copy

    # Catalog not blanked: unaffected tool is also present.
    assert "resources.list" in result_names

    # Observability: counter incremented exactly once, warning logged.
    assert counter_calls == [1]
    assert "provider-schema projection failed" in caplog.text
