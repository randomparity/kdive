"""app.py: tool registration via the seam, with an injected verifier."""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from typing import Any, cast

import pytest
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

import kdive.mcp.assembly.app as app_module
import kdive.mcp.assembly.tool_registration as tool_module
import kdive.mcp.schema.schema_advertising as envelope_module
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import assembly as handler_module
from kdive.jobs.assembly import build_handler_registry
from kdive.jobs.models import HandlerRegistry
from kdive.mcp.assembly.app import build_app
from kdive.providers.assembly import composition
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly, build_object_store_assembly
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair


def _verifier() -> JWTVerifier:
    kp = make_keypair()
    return JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)


def test_build_app_registers_jobs_tools() -> None:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())

    async def _run() -> None:
        # Verified against fastmcp 3.4.0: FastMCP.list_tools() is async and returns
        # list[Tool], each with a .name (there is no get_tools()).
        tools = await app.list_tools()
        names = {t.name for t in tools}
        assert {"jobs.get", "jobs.wait", "jobs.cancel", "jobs.list"} <= names
        assert {
            "systems.provision",
            "systems.provision_defined",
            "systems.get",
            "systems.teardown",
            "systems.reprovision",
        } <= names
        assert {
            "investigations.open",
            "investigations.get",
            "investigations.close",
            "investigations.link",
            "investigations.unlink",
        } <= names
        assert {
            "runs.create",
            "runs.get",
            "runs.complete_build",
            "runs.install",
            "runs.boot",
            "runs.cancel",
        } <= names
        assert {"control.power", "control.force_crash"} <= names
        assert {
            "vmcore.fetch",
            "vmcore.list",
            "artifacts.list",
            "artifacts.get",
            "artifacts.find",
            "postmortem.crash",
            "postmortem.triage",
        } <= names
        assert {"debug.start_session", "debug.end_session"} <= names
        assert {
            "debug.set_breakpoint",
            "debug.clear_breakpoint",
            "debug.list_breakpoints",
            "debug.read_memory",
            "debug.read_registers",
            "debug.continue",
            "debug.interrupt",
        } <= names
        assert {"introspect.from_vmcore", "introspect.run"} <= names
        assert {
            "accounting.estimate",
            "accounting.usage_project",
            "accounting.usage_investigation",
            "accounting.report_granted_set",
            "accounting.report_all_projects",
        } <= names
        assert {
            "reports.generate_granted_set",
            "reports.generate_all_projects",
        } <= names
        assert {
            "allocations.request",
            "allocations.get",
            "allocations.release",
            "allocations.renew",
            "allocations.list",
        } <= names

    asyncio.run(_run())


def test_resource_host_and_mutation_tools_are_registered() -> None:
    pool = AsyncConnectionPool("postgresql://unused", open=False)

    async def _run() -> None:
        app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
        names = {tool.name for tool in await app.list_tools()}
        assert {
            "resources.set_status",
            "resources.cordon",
            "resources.uncordon",
            "resources.drain",
            "resources.register_remote_libvirt",
            "resources.register_local_libvirt",
            "resources.register_fault_inject",
            "resources.deregister",
            "resources.renew",
        } <= names

    asyncio.run(_run())


def test_build_app_registers_doc_resources() -> None:
    # ADR-0151: build_app registers the operator docs the tool surface cites as MCP
    # resources, so ListMcpResources returns them and each reads back the canonical doc.
    from pathlib import Path

    from kdive.mcp.resources.registrar import DOC_RESOURCES

    repo_root = Path(__file__).resolve().parents[3]
    assert (repo_root / "docs").is_dir(), "repo-root resolution is wrong"

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())

    async def _run() -> None:
        listed = {str(r.uri) for r in await app.list_resources()}
        assert {e.uri for e in DOC_RESOURCES} <= listed
        for entry in DOC_RESOURCES:
            result = await app.read_resource(entry.uri)
            served = result.contents[0].content
            assert isinstance(served, str)
            canonical = (repo_root / entry.source).read_text(encoding="utf-8")
            assert served == canonical

    asyncio.run(_run())


def test_binding_error_middleware_is_registered_innermost() -> None:
    # BindingErrorMiddleware must sit after Telemetry + DenialAudit so a binding ValidationError
    # is converted to a returned envelope inside the telemetry span (ADR-0124; ADR-0132).
    from kdive.mcp.middleware.binding_errors import BindingErrorMiddleware
    from kdive.mcp.middleware.denial_audit import DenialAuditMiddleware
    from kdive.mcp.middleware.telemetry import TelemetryMiddleware

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
    order = [type(m).__name__ for m in app.middleware]
    assert order.index(BindingErrorMiddleware.__name__) > order.index(
        DenialAuditMiddleware.__name__
    )
    assert order.index(DenialAuditMiddleware.__name__) > order.index(TelemetryMiddleware.__name__)


def test_build_app_produces_a_streamable_http_asgi_app() -> None:
    # The server entrypoint serves build_app(...).http_app() over streamable HTTP;
    # assert the ASGI app assembles (no DB/network needed) so the run path is covered
    # beyond tool registration.
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
    asgi = app.http_app()
    assert callable(asgi)


def test_build_app_uses_injected_composition_secret_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[app_module.AppAssembly] = []

    def _build_registrars(assembly: app_module.AppAssembly) -> tuple[tool_module.PlaneRegistrar]:
        captured.append(assembly)

        def _register(app: FastMCP, _pool: AsyncConnectionPool) -> None:
            # Register one tool so build_app produces a non-empty surface — a real registrar always
            # registers tools, and build_app's flat-schema sweep raises on zero tools (ADR-0113).
            @app.tool(name="_probe")
            def _probe() -> str:
                return "ok"

        return (_register,)

    monkeypatch.setattr(app_module, "build_plane_registrars", _build_registrars)
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    composition_registry = SecretRegistry()
    caller_registry = SecretRegistry()
    provider_composition = composition.ProviderComposition(secret_registry=composition_registry)

    build_app(
        pool,
        verifier=_verifier(),
        provider_composition=provider_composition,
        secret_registry=caller_registry,
    )

    assert captured[0].secret_registry is composition_registry


def test_ops_images_registration_uses_standard_register_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = FastMCP("probe")
    store = object()
    captured: dict[str, object] = {}

    def _register(
        registered_app: FastMCP,
        registered_pool: AsyncConnectionPool,
        *,
        image_store: object | None,
        upload_store: object | None = None,
    ) -> None:
        captured["app"] = registered_app
        captured["pool"] = registered_pool
        captured["image_store"] = image_store
        captured["upload_store"] = upload_store

    monkeypatch.setattr(tool_module.ops_images_tools, "register", _register)
    object_stores = ObjectStoreAssembly(store=cast(Any, store))

    tool_module._ops_images_tools_registrar(object_stores)(app, pool)

    assert not hasattr(tool_module.ops_images_tools, "register_from_env")
    assert captured == {
        "app": app,
        "pool": pool,
        "image_store": store,
        "upload_store": store,
    }


def test_object_store_assembly_preserves_configured_store_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = CategorizedError(
        "invalid S3 endpoint",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"setting": "KDIVE_S3_ENDPOINT_URL"},
    )

    def _raise_store() -> object:
        raise error

    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "not-a-url")
    monkeypatch.setattr("kdive.store.assembly.object_store_from_env", _raise_store)

    with pytest.raises(CategorizedError) as caught:
        build_object_store_assembly()

    assert caught.value is error


def test_build_handler_registry_binds_provisioning_and_build_handlers() -> None:
    # The provisioning plane (#16) registers provision/teardown, the install + boot plane (#19)
    # registers install/boot, and the retrieve plane (#24) registers capture_vmcore — each
    # building its provider lazily from env (no libvirt/S3 connection at registration).
    registry = build_handler_registry(secret_registry=SecretRegistry())
    assert isinstance(registry, HandlerRegistry)
    assert registry.get(JobKind.PROVISION) is not None
    assert registry.get(JobKind.TEARDOWN) is not None
    assert registry.get(JobKind.INSTALL) is not None
    assert registry.get(JobKind.BOOT) is not None
    assert registry.get(JobKind.CAPTURE_VMCORE) is not None
    # The diagnostics worker-vantage dispatch plane (#514) binds its check-runner handler.
    assert registry.get(JobKind.DIAGNOSTICS_WORKER_CHECK) is not None
    # The control plane binds the diagnostic-sysrq and out-of-band crash-watch handlers (#984).
    assert registry.get(JobKind.DIAGNOSTIC_SYSRQ) is not None
    assert registry.get(JobKind.WATCH_FOR_CRASH) is not None


def test_build_handler_registry_derives_worker_ports_from_one_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = object()
    caller_registry = SecretRegistry()
    captured: dict[str, object | None] = {}

    class _FakeComposition:
        secret_registry = caller_registry

        def build_provider_resolver(self) -> object:
            return resolver

    def _build(
        assembly: handler_module.WorkerHandlerAssembly,
    ) -> tuple[handler_module.HandlerRegistrar, ...]:
        captured["resolver"] = assembly.resolver
        captured["secret_registry"] = assembly.secret_registry
        captured["object_stores"] = assembly.object_stores

        return ()

    monkeypatch.setattr(handler_module, "build_handler_registrars", _build)

    build_handler_registry(
        secret_registry=caller_registry,
        provider_composition=cast(Any, _FakeComposition()),
    )

    assert captured["resolver"] is resolver
    assert captured["secret_registry"] is caller_registry
    object_stores = captured["object_stores"]
    assert isinstance(object_stores, ObjectStoreAssembly)
    # S3 is a required backend (ADR-0337): the assembly carries one non-optional store.
    assert object_stores.store is not None


def test_core_tools_subset_of_registry() -> None:
    """Every CORE_TOOLS entry is a registered tool name (ADR-0268).

    A misspelled or stale entry would silently drop it from the default gateway listing.
    CORE_TOOLS must be a strict subset of the live registry; any missing name fails here.
    """
    from kdive.mcp.exposure import CORE_TOOLS

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
    registered = {t.name for t in envelope_module.registered_tools(app)}
    assert registered >= CORE_TOOLS, f"core not registered: {sorted(CORE_TOOLS - registered)}"


def test_exposure_map_covers_every_registered_tool() -> None:
    """Every registered tool is consciously triaged: gated (CLASSIFIED_TOOLS) or PUBLIC.

    `CLASSIFIED_TOOLS | PUBLIC_TOOLS` must equal the live registry, so a newly added tool
    trips this (it is in neither) and forces the author to classify it — the completeness
    guard for #506 / ADR-0148. No stale entries either (the union is exactly the registry).
    """
    from kdive.mcp.exposure import (
        CLASSIFIED_TOOLS,
        PUBLIC_TOOLS,
        ExposureScope,
        required_scopes,
    )

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())

    async def _run() -> set[str]:
        return {t.name for t in await app.list_tools()}

    registered = asyncio.run(_run())
    triaged = CLASSIFIED_TOOLS | PUBLIC_TOOLS
    assert triaged == registered, (
        f"untriaged (classify in exposure.py): {sorted(registered - triaged)}; "
        f"stale entries: {sorted(triaged - registered)}"
    )
    # Spot-pin a few that must stay gated (a regression to public would otherwise be silent).
    assert required_scopes("control.force_crash") == frozenset({ExposureScope.PROJECT_ADMIN})
    assert required_scopes("systems.teardown") == frozenset({ExposureScope.PROJECT_ADMIN})
    assert required_scopes("ops.reconcile_now") == frozenset({ExposureScope.PLATFORM_OPERATOR})
    # allocations.request (ADR-0234) and the provision lane (ADR-0326) are contributor;
    # images.upload stays operator (shared cross-tenant catalog).
    assert required_scopes("allocations.request") == frozenset({ExposureScope.PROJECT_CONTRIBUTOR})
    assert required_scopes("systems.define") == frozenset({ExposureScope.PROJECT_CONTRIBUTOR})
    assert required_scopes("images.upload") == frozenset({ExposureScope.PROJECT_OPERATOR})


# --- Canonical lifecycle prompts (ADR-0202) ---------------------------------------------

# Independent, human-reviewed expected maturity per referenced prompt step (the drift
# guard). A registry-vs-registry compare would be vacuous, so this table is asserted equal
# to the live registry; a promotion/demotion of any referenced tool fails here until the
# expectation is updated, making a journey's maturity shape a reviewed event.
_EXPECTED_STEP_MATURITY: dict[str, str] = {
    "investigations.open": "implemented",
    "resources.list": "implemented",
    "allocations.request": "implemented",
    "allocations.wait": "implemented",
    "systems.define": "implemented",
    "runs.create": "implemented",
    "runs.complete_build": "implemented",
    "runs.install": "implemented",
    "runs.boot": "implemented",
    "debug.start_session": "implemented",
    "introspect.run": "implemented",
    "debug.end_session": "implemented",
    "control.force_crash": "implemented",
    "vmcore.fetch": "implemented",
    "vmcore.list": "implemented",
    "postmortem.triage": "implemented",
    "introspect.from_vmcore": "implemented",
}


def _built_app() -> FastMCP:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    return build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())


def _rendered_prompt_body(app: FastMCP, name: str) -> str:
    from mcp.types import TextContent

    async def _run() -> str:
        result = await app.render_prompt(name, {})
        content = result.messages[0].content
        assert isinstance(content, TextContent)
        return content.text

    return asyncio.run(_run())


def test_build_app_registers_lifecycle_prompts() -> None:
    from kdive.mcp.prompts.registrar import CANONICAL_PROMPTS

    app = _built_app()

    async def _names() -> set[str]:
        return {p.name for p in await app.list_prompts()}

    listed = asyncio.run(_names())
    assert {spec.name for spec in CANONICAL_PROMPTS} <= listed
    for spec in CANONICAL_PROMPTS:
        body = _rendered_prompt_body(app, spec.name)
        for step in spec.steps:
            assert step.tool in body, f"{spec.name} body omits {step.tool}"


def test_lifecycle_prompts_disclose_no_partial_steps_when_all_implemented() -> None:
    # #816: with postmortem.crash/triage promoted, every tool the triage_panic journey
    # references is `implemented`, so the rendered body tags no step `[partial`. The
    # disclosure *rendering* (a partial step gets a `[partial: reason]` tag) is unit-tested
    # against a fabricated maturity map in tests/mcp/prompts/test_lifecycle_prompts.py; this
    # integration check pins the live registry's current all-implemented state.
    body = _rendered_prompt_body(_built_app(), "triage_panic")
    triage_line = next(line for line in body.splitlines() if "postmortem.triage " in line)
    assert "[partial" not in triage_line
    # No numbered step line carries a [partial tag (the _NOTES footer mentions it literally).
    step_lines = [ln for ln in body.splitlines() if " — " in ln]
    assert step_lines and all("[partial" not in ln for ln in step_lines)


def test_lifecycle_prompts_expected_maturity_matches_registry() -> None:
    app = _built_app()
    live = {
        tool.name: (tool.meta or {}).get("maturity", "implemented")
        for tool in envelope_module.registered_tools(app)
    }
    for tool, expected in _EXPECTED_STEP_MATURITY.items():
        assert tool in live, f"prompt references unregistered tool {tool!r}"
        assert live[tool] == expected, (
            f"{tool} maturity drifted: registry={live[tool]!r} expected={expected!r}; "
            "update _EXPECTED_STEP_MATURITY after reviewing the journey"
        )


def test_prompts_add_no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    # Graceful degradation: the prompts plane registers no tools and removes none.
    with_prompts = {t.name for t in asyncio.run(_built_app().list_tools())}

    def _without_prompts(
        assembly: app_module.AppAssembly,
    ) -> tuple[tool_module.PlaneRegistrar, ...]:
        registrars = tool_module.build_plane_registrars(assembly)
        without = tuple(r for r in registrars if r is not tool_module._register_lifecycle_prompts)
        assert len(without) == len(registrars) - 1
        return without

    monkeypatch.setattr(app_module, "build_plane_registrars", _without_prompts)
    without_prompts = {t.name for t in asyncio.run(_built_app().list_tools())}

    assert with_prompts == without_prompts


def test_compact_middleware_is_registered_outermost() -> None:
    # CompactResponseMiddleware must be outer of DenialAudit + BindingError so it observes their
    # synthesized failure envelopes; first-added is outermost (ADR-0314).
    from kdive.mcp.middleware.binding_errors import BindingErrorMiddleware
    from kdive.mcp.middleware.compact import CompactResponseMiddleware
    from kdive.mcp.middleware.denial_audit import DenialAuditMiddleware

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
    order = [type(m).__name__ for m in app.middleware]
    assert order.index(CompactResponseMiddleware.__name__) < order.index(
        DenialAuditMiddleware.__name__
    )
    assert order.index(CompactResponseMiddleware.__name__) < order.index(
        BindingErrorMiddleware.__name__
    )


def test_build_app_logs_when_compact_enabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("KDIVE_COMPACT_RESPONSES", "on")
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    with caplog.at_level("INFO", logger="kdive.mcp.assembly.app"):
        build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
    assert sum("compact_responses enabled" in r.getMessage() for r in caplog.records) == 1


def test_build_app_silent_when_compact_disabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("KDIVE_COMPACT_RESPONSES", raising=False)
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    with caplog.at_level("INFO", logger="kdive.mcp.assembly.app"):
        build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
    assert not any("compact_responses enabled" in r.getMessage() for r in caplog.records)


# --- Tool-name reference resolution guard (#1365) ---------------------------------------
#
# A tool rename or a stale hand-authored reference used to drift silently: nothing
# cross-checked the tool names embedded in `suggested_next_actions` literals, in
# backtick-quoted tokens inside served docstrings/docs, or in prompt steps against the
# live `list_tools()` registry. These guards make any orphaned reference fail CI.

# A registered tool name is `namespace.verb` (both lowercase, snake segments).
_TOOL_NAME_SHAPE = re.compile(r"^[a-z][a-z_]*\.[a-z][a-z_]*$")

# Tokens that share the `namespace.verb` shape but are deliberately not tools: the operator
# config filename and the `debug.gdbstub` live-debug boot preset. Listing them keeps the
# backtick-reference guard generic (it checks every tool-shaped token whose namespace is
# live) while acknowledging these known collisions. A genuine rename still fails the guard.
_NON_TOOL_TOKENS = frozenset({"systems.toml", "debug.gdbstub"})

_BACKTICK = re.compile(r"`([^`]+)`")


def _live_tool_names() -> set[str]:
    async def _run() -> set[str]:
        return {t.name for t in await _built_app().list_tools()}

    return asyncio.run(_run())


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Map module-level ``NAME = "literal"`` assignments to their string value.

    Many call sites pass a module constant (``_GRANTED_TOOL``) rather than an inline
    string to ``suggested_next_actions``; resolving them lets the guard bite when the
    constant's tool name goes stale.
    """
    consts: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                consts[target.id] = node.value.value
    return consts


def _suggested_actions_value(node: ast.AST) -> ast.AST | None:
    """Return the value node assigned to ``suggested_next_actions``, if this node is one."""
    if isinstance(node, ast.keyword) and node.arg == "suggested_next_actions":
        return node.value
    if isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "suggested_next_actions" for t in node.targets
    ):
        return node.value
    return None


def _string_literals(value: ast.AST, consts: dict[str, str]) -> list[str]:
    """Collect string literals reachable from ``value``, resolving module constants.

    Walks lists and conditional expressions; a ``Name`` bound to a module-level string
    constant resolves to its value. Dynamic parts (f-strings, parameters, locals) yield
    nothing and are intentionally not checked — they are not literals.
    """
    out: list[str] = []
    for sub in ast.walk(value):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
        elif isinstance(sub, ast.Name) and sub.id in consts:
            out.append(consts[sub.id])
    return out


def _suggested_next_action_literals() -> dict[str, str]:
    """Map every tool-shaped ``suggested_next_actions`` literal to a source file path."""
    mcp_src = Path(__file__).resolve().parents[3] / "src" / "kdive" / "mcp"
    assert mcp_src.is_dir(), "repo-root resolution is wrong"
    found: dict[str, str] = {}
    for py in mcp_src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "suggested_next_actions" not in text:
            continue
        tree = ast.parse(text)
        consts = _module_string_constants(tree)
        for node in ast.walk(tree):
            value = _suggested_actions_value(node)
            if value is None:
                continue
            for literal in _string_literals(value, consts):
                if _TOOL_NAME_SHAPE.match(literal):
                    found.setdefault(literal, str(py))
    return found


def test_suggested_next_actions_resolve_to_live_tools() -> None:
    """Every tool-shaped ``suggested_next_actions`` literal is a live tool name (#1365).

    A rename that misses a hand-authored ``suggested_next_actions=["images.describe"]``
    would silently steer an agent at a dead tool; this fails until it is fixed.
    """
    registered = _live_tool_names()
    literals = _suggested_next_action_literals()
    stale = sorted(f"{name!r} ({src})" for name, src in literals.items() if name not in registered)
    assert not stale, "suggested_next_actions references not in live registry:\n" + "\n".join(stale)


def _served_docs(app: FastMCP) -> list[tuple[str, str]]:
    """Return (label, body) for every served docstring, the instructions, and each doc."""
    from kdive.mcp.resources.registrar import DOC_RESOURCES

    repo_root = Path(__file__).resolve().parents[3]
    sources: list[tuple[str, str]] = []
    for tool in asyncio.run(app.list_tools()):
        if tool.description:
            sources.append((f"tool:{tool.name} docstring", tool.description))
    sources.append(("server instructions", app.instructions or ""))
    for entry in DOC_RESOURCES:
        sources.append((f"doc:{entry.source}", (repo_root / entry.source).read_text("utf-8")))
    return sources


def test_backtick_tool_refs_in_docs_resolve_to_live_tools() -> None:
    """Every backtick tool-name in a served docstring/doc resolves to a live tool (#1365).

    Restricted to backtick tokens of ``namespace.verb`` shape whose namespace is live, so a
    stale ``debug.old_name`` reference fails while prose like ``maturity_detail`` is ignored.
    Known tool-shaped non-tools (``systems.toml``) are allowlisted in ``_NON_TOOL_TOKENS``.
    """
    app = _built_app()
    registered = {tool.name for tool in asyncio.run(app.list_tools())}
    namespaces = {name.split(".")[0] for name in registered}
    stale: set[str] = set()
    for label, body in _served_docs(app):
        for match in _BACKTICK.finditer(body):
            token = match.group(1).strip()
            if not (_TOOL_NAME_SHAPE.match(token) and token.split(".")[0] in namespaces):
                continue
            if token in _NON_TOOL_TOKENS or token in registered:
                continue
            stale.add(f"{token!r} ({label})")
    assert not stale, "backtick tool references not in live registry:\n" + "\n".join(sorted(stale))


def test_prompt_steps_resolve_to_live_tools() -> None:
    """Every canonical-prompt ``Step`` tool is a live tool name (#1365).

    ``build_app`` already fails fast on an unknown step (registrar ``_step_line``); this
    pins the contract explicitly against the whole ``CANONICAL_PROMPTS`` set, not the curated
    ``_EXPECTED_STEP_MATURITY`` subset.
    """
    from kdive.mcp.prompts.registrar import CANONICAL_PROMPTS

    registered = _live_tool_names()
    stale = sorted(
        f"{step.tool!r} ({spec.name})"
        for spec in CANONICAL_PROMPTS
        for step in spec.steps
        if step.tool not in registered
    )
    assert not stale, "prompt steps not in live registry:\n" + "\n".join(stale)
