"""app.py: tool registration via the seam, with an injected verifier."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

import kdive.mcp.app as app_module
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import JobKind
from kdive.jobs.models import HandlerRegistry
from kdive.mcp.app import build_app, build_handler_registry
from kdive.providers.assembly import composition
from kdive.security.secrets.secret_registry import SecretRegistry
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
            "runs.build",
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
    from kdive.mcp.middleware import (
        BindingErrorMiddleware,
        DenialAuditMiddleware,
        TelemetryMiddleware,
    )

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

    def _capture_assembly(
        app: FastMCP,
        _pool: AsyncConnectionPool,
        assembly: app_module.AppAssembly,
    ) -> None:
        captured.append(assembly)

        # Register one tool so build_app produces a non-empty surface — a real registrar always
        # registers tools, and build_app's flat-schema sweep raises on a zero-tool count (ADR-0113).
        @app.tool(name="_probe")
        def _probe() -> str:
            return "ok"

    monkeypatch.setattr(app_module, "_PLANE_REGISTRARS", (_capture_assembly,))
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

    def _store_from_env() -> object:
        return store

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

    monkeypatch.setattr("kdive.store.objectstore.object_store_from_env", _store_from_env)
    monkeypatch.setattr(app_module.ops_images_tools, "register", _register)

    app_module._register_ops_images_tools(app, pool, cast(Any, None))

    assert not hasattr(app_module.ops_images_tools, "register_from_env")
    assert captured == {
        "app": app,
        "pool": pool,
        "image_store": store,
        "upload_store": store,
    }


def test_ops_images_store_resolver_preserves_configured_store_error(
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
    monkeypatch.setattr("kdive.store.objectstore.object_store_from_env", _raise_store)

    with pytest.raises(CategorizedError) as caught:
        app_module._resolve_ops_images_store()

    assert caught.value is error


def test_build_handler_registry_binds_provisioning_and_build_handlers() -> None:
    # The provisioning plane (#16) registers provision/teardown, the build plane (#18)
    # registers build, the install + boot plane (#19) registers install/boot, and the
    # retrieve plane (#24) registers capture_vmcore — each building its provider/builder
    # lazily from env (no libvirt/S3/toolchain connection at registration).
    registry = build_handler_registry(secret_registry=SecretRegistry())
    assert isinstance(registry, HandlerRegistry)
    assert registry.get(JobKind.PROVISION) is not None
    assert registry.get(JobKind.TEARDOWN) is not None
    assert registry.get(JobKind.BUILD) is not None
    assert registry.get(JobKind.INSTALL) is not None
    assert registry.get(JobKind.BOOT) is not None
    assert registry.get(JobKind.CAPTURE_VMCORE) is not None


def test_build_handler_registry_derives_worker_ports_from_one_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = object()
    transports = object()
    caller_registry = SecretRegistry()
    captured: dict[str, object | None] = {}

    class _FakeComposition:
        def build_provider_resolver(self) -> object:
            return resolver

        def build_build_host_transport_factories(self) -> object:
            return transports

    def _capture(
        _registry: HandlerRegistry,
        provider_resolver: object,
        secret_registry: SecretRegistry,
        build_host_transport_factories: object | None,
    ) -> None:
        captured["resolver"] = provider_resolver
        captured["secret_registry"] = secret_registry
        captured["transports"] = build_host_transport_factories

    monkeypatch.setattr(app_module, "_HANDLER_REGISTRARS", (_capture,))

    build_handler_registry(
        secret_registry=caller_registry,
        provider_composition=cast(Any, _FakeComposition()),
    )

    assert captured == {
        "resolver": resolver,
        "secret_registry": caller_registry,
        "transports": transports,
    }


def test_image_build_handler_preserves_store_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = HandlerRegistry()
    error = CategorizedError(
        "missing image store",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"setting": "KDIVE_S3_ENDPOINT"},
    )

    def _raise_store() -> object:
        raise error

    monkeypatch.setattr("kdive.store.objectstore.object_store_from_env", _raise_store)
    app_module._register_image_build_handler(
        registry, cast(Any, None), SecretRegistry(), cast(Any, None)
    )
    handler = registry.get(JobKind.IMAGE_BUILD)
    assert handler is not None

    async def _run() -> None:
        with pytest.raises(CategorizedError) as caught:
            await handler(cast(Any, None), cast(Any, None))
        assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert caught.value.details == {"setting": "KDIVE_S3_ENDPOINT"}
        assert caught.value.__cause__ is error

    asyncio.run(_run())


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
    assert required_scopes("allocations.request") == frozenset({ExposureScope.PROJECT_OPERATOR})
