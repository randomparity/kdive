"""introspect.from_vmcore tool tests — the handler is called directly with a fake port."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastmcp import Client, FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import DEBUG_SESSIONS
from kdive.domain.capacity.state import DebugSessionState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import DebugSession
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.debug import introspect as introspect_tools
from kdive.prereqs.system_bootstrap_key import ensure_system_bootstrap_key
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.providers.ports.retrieve import (
    IntrospectOutput,
    LiveScriptOutput,
)
from kdive.security.authz.rbac import AuthorizationError, Role
from tests.mcp._seed import seed_crashed_system, seed_run_on_system
from tests.mcp.json_data import data_mapping, json_mapping, json_sequence


def _ctx(
    role: Role | None = Role.VIEWER, *, projects: tuple[str, ...] = ("proj",)
) -> RequestContext:
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="u", agent_session="s", projects=projects, roles=roles)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _output(*, comm: str = "init", truncated: bool = False) -> IntrospectOutput:
    return IntrospectOutput(
        tasks={"tasks": [{"pid": 1, "comm": comm}], "truncated": False},
        modules={"modules": [], "decode_errors": 0, "all_failed": False},
        sysinfo={"release": "6.8.0"},
        truncated=truncated,
    )


class _FakeIntrospector:
    """Records the from_vmcore kwargs; returns a canned output or raises a planted error."""

    def __init__(
        self, *, output: IntrospectOutput | None = None, raises: CategorizedError | None = None
    ) -> None:
        self._output = output if output is not None else _output()
        self._raises = raises
        self.kwargs: dict[str, object] = {}

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        self.kwargs = {
            "vmcore_ref": vmcore_ref,
            "debuginfo_ref": debuginfo_ref,
            "expected_build_id": expected_build_id,
        }
        if self._raises is not None:
            raise self._raises
        return self._output


async def _seed_vmcore_row(pool: AsyncConnectionPool, run_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
            "retention_class) VALUES ('runs', %s, %s, 'e', 'sensitive', 'vmcore')",
            (run_id, f"local/runs/{run_id}/vmcore-host_dump"),
        )


async def _built_run_with_core(pool: AsyncConnectionPool) -> str:
    sys_id = await seed_crashed_system(pool)
    run_id = await seed_run_on_system(
        pool, sys_id, debuginfo_ref="k/runs/r/vmlinux", build_id="deadbeef"
    )
    await _seed_vmcore_row(pool, run_id)
    return run_id


def test_from_vmcore_happy_path_returns_redacted_report(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            port = _FakeIntrospector()
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id=run_id, introspector=port
            )
        assert resp.status != "error"
        report = data_mapping(resp, "report")
        sysinfo = json_mapping(report["sysinfo"])
        assert sysinfo["release"] == "6.8.0"
        assert resp.data["truncated"] is False
        assert port.kwargs["expected_build_id"] == "deadbeef"
        assert port.kwargs["debuginfo_ref"] == "k/runs/r/vmlinux"
        assert str(port.kwargs["vmcore_ref"]).endswith("/vmcore-host_dump")

    asyncio.run(_run())


def test_from_vmcore_surfaces_truncated_true(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            port = _FakeIntrospector(output=_output(truncated=True))
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id=run_id, introspector=port
            )
        assert resp.status != "error"
        assert resp.data["truncated"] is True

    asyncio.run(_run())


def test_from_vmcore_passes_through_port_redacted_report(migrated_url: str) -> None:
    # The port is the single redaction boundary (ADR-0033 §6); the handler serializes the
    # already-redacted report verbatim. A real port returns `[REDACTED]` in place of secrets;
    # here the fake supplies that redacted shape and the handler must surface it unchanged.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            port = _FakeIntrospector(output=_output(comm="[REDACTED]"))
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id=run_id, introspector=port
            )
        assert resp.status != "error"
        report = data_mapping(resp, "report")
        tasks = json_mapping(report["tasks"])
        first_task = json_mapping(json_sequence(tasks["tasks"])[0])
        assert first_task["comm"] == "[REDACTED]"

    asyncio.run(_run())


def test_from_vmcore_never_booted_reports_no_vmcore(migrated_url: str) -> None:
    # A never-booted run lacks debuginfo, build, AND a captured core. The introspect tool is
    # vmcore-centric, so the operative gap (no_vmcore) surfaces first, not the earliest-unmet
    # build precondition (#553, ADR-0165).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id=run_id, introspector=_FakeIntrospector()
            )
        assert resp.status == "error" and resp.error_category == "not_found"
        assert resp.data["reason"] == "no_vmcore"
        assert resp.suggested_next_actions == ["vmcore.fetch", "runs.get"]

    asyncio.run(_run())


def test_from_vmcore_core_present_null_debuginfo_is_no_debuginfo(migrated_url: str) -> None:
    # A run with a captured core but a null debuginfo_ref still reports the precise no_debuginfo
    # reason: the reorder only moves no_vmcore ahead, it does not collapse the distinct reasons.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id="deadbeef")
            await _seed_vmcore_row(pool, run_id)
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id=run_id, introspector=_FakeIntrospector()
            )
        assert resp.status == "error" and resp.error_category == "not_found"
        assert resp.data["reason"] == "no_debuginfo"
        assert resp.suggested_next_actions == ["runs.get", "runs.complete_build"]

    asyncio.run(_run())


def test_from_vmcore_no_build_step_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(
                pool, sys_id, debuginfo_ref="k/runs/r/vmlinux", build_id=None
            )
            await _seed_vmcore_row(pool, run_id)
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id=run_id, introspector=_FakeIntrospector()
            )
        assert resp.status == "error" and resp.error_category == "not_found"
        assert resp.data["reason"] == "no_build"
        assert resp.suggested_next_actions == ["runs.complete_build", "runs.get"]

    asyncio.run(_run())


def test_from_vmcore_no_captured_core_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await seed_crashed_system(pool)
            run_id = await seed_run_on_system(
                pool, sys_id, debuginfo_ref="k/runs/r/vmlinux", build_id="deadbeef"
            )
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id=run_id, introspector=_FakeIntrospector()
            )
        assert resp.status == "error" and resp.error_category == "not_found"
        assert resp.data["reason"] == "no_vmcore"
        assert resp.suggested_next_actions == ["vmcore.fetch", "runs.get"]

    asyncio.run(_run())


def test_from_vmcore_malformed_run_id_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id="nope", introspector=_FakeIntrospector()
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_from_vmcore_cross_project_is_not_found(migrated_url: str) -> None:
    # No-leak: a run in an ungranted project is indistinguishable from absent (not_found).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(projects=("other",)), run_id=run_id, introspector=_FakeIntrospector()
            )
        assert resp.status == "error" and resp.error_category == "not_found"
        # No-leak: the cross-project miss carries no precondition reason or next actions, so the
        # envelope is byte-identical to a genuinely-absent run (#487).
        assert "reason" not in resp.data
        assert resp.suggested_next_actions == []

    asyncio.run(_run())


def test_from_vmcore_without_viewer_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            with pytest.raises(AuthorizationError):
                await introspect_tools.introspect_from_vmcore(
                    pool, _ctx(None), run_id=run_id, introspector=_FakeIntrospector()
                )

    asyncio.run(_run())


def test_from_vmcore_port_attach_failure_is_typed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            err = CategorizedError("drgn", category=ErrorCategory.DEBUG_ATTACH_FAILURE)
            port = _FakeIntrospector(raises=err)
            resp = await introspect_tools.introspect_from_vmcore(
                pool, _ctx(), run_id=run_id, introspector=port
            )
        assert resp.status == "error" and resp.error_category == "debug_attach_failure"

    asyncio.run(_run())


async def _call_registered_tool(
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    *,
    tool: str,
    arguments: dict[str, object],
    ctx: RequestContext,
    monkeypatch: pytest.MonkeyPatch,
) -> ToolResponse:
    """Register the introspect tools and invoke one through the FastMCP transport (wrapper path)."""
    monkeypatch.setattr(introspect_tools, "current_context", lambda: ctx)
    app: FastMCP = FastMCP(name="t")
    introspect_tools.register(app, pool, resolver=resolver)
    async with Client(app) as client:
        result = await client.call_tool(tool, arguments, raise_on_error=False)
    assert result.structured_content is not None
    return ToolResponse.model_validate(result.structured_content)


def test_from_vmcore_unsupported_plane_is_capability_unsupported(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-0209: a provider whose descriptor lacks offline-vmcore introspection rejects
    # introspect.from_vmcore up front with capability_unsupported; the drgn port is never called.
    from tests.mcp.systems_support import provider_resolver

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            resolver = provider_resolver(supported_introspection=frozenset())
            resp = await _call_registered_tool(
                pool,
                resolver,
                tool="introspect.from_vmcore",
                arguments={"run_id": run_id},
                ctx=_ctx(),
                monkeypatch=monkeypatch,
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "capability_unsupported"
        assert resp.data["capability"] == "introspection:offline-vmcore"
        assert resp.data["supported"] == []

    asyncio.run(_run())


def test_from_vmcore_admitted_when_offline_vmcore_supported(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-0209/0210 B2: a local provider advertising offline-vmcore introspection admits
    # introspect.from_vmcore — the gate passes and the wired port runs, returning the report.
    from tests.mcp.systems_support import provider_resolver

    port = _FakeIntrospector()

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id = await _built_run_with_core(pool)
            resolver = provider_resolver(
                supported_introspection=frozenset({"offline-vmcore"}),
                vmcore_introspector=port,
            )
            resp = await _call_registered_tool(
                pool,
                resolver,
                tool="introspect.from_vmcore",
                arguments={"run_id": run_id},
                ctx=_ctx(),
                monkeypatch=monkeypatch,
            )
        assert resp.status == "succeeded"
        assert "report" in resp.data
        # The gate did not short-circuit: the wired port was actually invoked.
        assert port.kwargs["expected_build_id"] == "deadbeef"

    asyncio.run(_run())


def test_run_unsupported_live_plane_is_capability_unsupported(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-0209: a provider whose descriptor lacks live introspection rejects introspect.run up
    # front with capability_unsupported; the live drgn port is never called.
    from tests.mcp.systems_support import provider_resolver

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool)
            resolver = provider_resolver(supported_introspection=frozenset())
            resp = await _call_registered_tool(
                pool,
                resolver,
                tool="introspect.run",
                arguments={"session_id": session_id, "helper": "tasks"},
                ctx=_live_ctx(),
                monkeypatch=monkeypatch,
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "capability_unsupported"
        assert resp.data["capability"] == "introspection:live"
        assert resp.data["supported"] == []

    asyncio.run(_run())


def test_register_adds_the_tool() -> None:
    from fastmcp import FastMCP

    async def _check() -> None:
        app: FastMCP = FastMCP(name="t")
        pool = AsyncConnectionPool("postgresql://unused", open=False)
        runtime = cast(
            ProviderRuntime,
            SimpleNamespace(
                vmcore_introspector=_FakeIntrospector(),
                live_introspector=_FakeLiveIntrospector(),
            ),
        )
        resolver = ProviderResolver({ResourceKind.LOCAL_LIBVIRT: runtime})
        introspect_tools.register(app, pool, resolver=resolver)
        tools = await app.list_tools()
        names = {t.name for t in tools}
        assert "introspect.from_vmcore" in names
        assert "introspect.run" in names

    asyncio.run(_check())


# --- introspect.run (live drgn over ssh, ADR-0039) -----------------------------------------


def _live_ctx(role: Role | None = Role.OPERATOR, *, projects: tuple[str, ...] = ("proj",)):
    roles = {"proj": role} if role is not None else {}
    return RequestContext(principal="u", agent_session="s", projects=projects, roles=roles)


class _FakeLiveIntrospector:
    """Records live introspection input; returns a canned output or raises a planted error.

    ``key_path`` is recorded separately from ``kwargs`` (in ``key_paths_seen``) since its value is
    a fresh mkdtemp path on every call — callers that don't care about the exact path keep
    asserting ``kwargs == {...}`` for the stable fields, and separately assert a temp key path was
    passed (non-empty, and — since the tool materializes-then-removes it — no longer on disk by
    the time the call returns).
    """

    def __init__(
        self, *, output: IntrospectOutput | None = None, raises: CategorizedError | None = None
    ) -> None:
        self._output = output if output is not None else _output()
        self._raises = raises
        self.kwargs: dict[str, object] = {}
        self.key_paths_seen: list[str] = []

    def introspect_live(
        self, *, transport_handle: str, helper: str, key_path: str
    ) -> IntrospectOutput:
        self.kwargs = {"transport_handle": transport_handle, "helper": helper}
        self.key_paths_seen.append(key_path)
        if self._raises is not None:
            raise self._raises
        return self._output

    def run_script(
        self, *, transport_handle: str, script: str, timeout_sec: float, key_path: str
    ) -> LiveScriptOutput:
        self.kwargs = {
            "transport_handle": transport_handle,
            "script": script,
            "timeout_sec": timeout_sec,
        }
        self.key_paths_seen.append(key_path)
        if self._raises is not None:
            raise self._raises
        return LiveScriptOutput(output="ok", truncated=False)


class _CountingResolver(ProviderResolver):
    def __init__(self, runtime: ProviderRuntime) -> None:
        super().__init__({ResourceKind.LOCAL_LIBVIRT: runtime})
        self.calls: list[UUID] = []
        self._runtime = runtime

    async def runtime_for_session(self, conn: AsyncConnection, session_id: UUID) -> ProviderRuntime:
        del conn
        self.calls.append(session_id)
        return self._runtime


def _live_resolver(
    port: _FakeLiveIntrospector,
    *,
    supported_introspection: frozenset[str] = frozenset({"live", "live-script"}),
) -> _CountingResolver:
    runtime = cast(
        ProviderRuntime,
        SimpleNamespace(
            vmcore_introspector=_FakeIntrospector(),
            live_introspector=port,
            supported_introspection=supported_introspection,
            component_sources=SimpleNamespace(provider="local-libvirt"),
        ),
    )
    return _CountingResolver(runtime)


async def _seed_live_drgn_session(
    pool: AsyncConnectionPool,
    *,
    state: DebugSessionState = DebugSessionState.LIVE,
    transport: str = "drgn-live",
    transport_handle: str | None = None,
    project: str = "proj",
    with_bootstrap_key: bool = True,
    debuginfo_ref: str | None = "k/runs/r/vmlinux",
) -> str:
    """Seed a DebugSession on a crashed System; by default also seeds its bootstrap key row.

    ``with_bootstrap_key=False`` seeds a session on a System with no `system_bootstrap_keys` row,
    for testing the fail-closed CONFIGURATION_ERROR path (ADR-0289). ``debuginfo_ref=None`` seeds a
    Run with no uploaded host vmlinux, so the ADR-0322 debuginfo warning is not suppressed.
    """
    sys_id = await seed_crashed_system(pool, project=project)
    run_id = await seed_run_on_system(
        pool, sys_id, debuginfo_ref=debuginfo_ref, build_id="deadbeef", project=project
    )
    if with_bootstrap_key:
        async with pool.connection() as conn:
            await ensure_system_bootstrap_key(conn, UUID(sys_id))
    handle = transport_handle if transport_handle is not None else f"{transport}://127.0.0.1:22"
    async with pool.connection() as conn:
        session = await DEBUG_SESSIONS.insert(
            conn,
            DebugSession(
                id=uuid4(),
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                principal="u",
                project=project,
                run_id=UUID(run_id),
                state=state,
                transport=transport,
                transport_handle=handle,
            ),
        )
    return str(session.id)


def test_run_live_routes_bare_domain_handle_to_introspector(migrated_url: str) -> None:
    # ADR-0083 §4: a remote drgn-live session's handle is the bare domain name; introspect.run
    # must pass it through verbatim to the live introspector (#215).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool, transport_handle="kdive-remote-1")
            port = _FakeLiveIntrospector()
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id=session_id,
                helper="tasks",
                resolver=_live_resolver(port),
            )
        assert resp.status != "error"
        assert port.kwargs == {"transport_handle": "kdive-remote-1", "helper": "tasks"}

    asyncio.run(_run())


def test_run_live_loads_and_materializes_the_per_system_bootstrap_key(migrated_url: str) -> None:
    """introspect.run loads the System's bootstrap key and passes a materialized temp path down.

    Mirrors the ssh_authorize handler test (tests/jobs/handlers/test_ssh_authorize.py): the engine
    receives a real, existing key file at call time; by the time the tool call returns, the temp
    key has been removed (the `with materialized_private_key(...)` scope wraps the engine call).
    """

    class _RecordingIntrospector(_FakeLiveIntrospector):
        def __init__(self) -> None:
            super().__init__()
            self.key_path_existed_during_call = False

        def introspect_live(
            self, *, transport_handle: str, helper: str, key_path: str
        ) -> IntrospectOutput:
            self.key_path_existed_during_call = Path(key_path).is_file()
            return super().introspect_live(
                transport_handle=transport_handle, helper=helper, key_path=key_path
            )

    async def _run() -> tuple[ToolResponse, _RecordingIntrospector]:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool)
            port = _RecordingIntrospector()
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id=session_id,
                helper="tasks",
                resolver=_live_resolver(port),
            )
        return resp, port

    resp, port = asyncio.run(_run())
    assert resp.status != "error"
    assert port.key_path_existed_during_call is True
    assert len(port.key_paths_seen) == 1
    key_path = port.key_paths_seen[0]
    assert key_path  # a real path was passed, not empty/None
    assert not Path(key_path).exists()  # removed after the call (materialized_private_key scope)


def test_run_live_no_bootstrap_key_is_configuration_error(migrated_url: str) -> None:
    """A System with no `system_bootstrap_keys` row fails closed (ADR-0289) before the SSH seam."""

    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool, with_bootstrap_key=False)
            port = _FakeLiveIntrospector()
            return await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id=session_id,
                helper="tasks",
                resolver=_live_resolver(port),
            )

    resp = asyncio.run(_run())
    assert resp.status == "error"
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR


def test_run_live_happy_path_returns_redacted_report(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool)
            port = _FakeLiveIntrospector()
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id=session_id,
                helper="tasks",
                resolver=_live_resolver(port),
            )
        assert resp.status != "error"
        report = data_mapping(resp, "report")
        assert set(report) == {"tasks"}
        tasks = json_mapping(report["tasks"])
        first_task = json_mapping(json_sequence(tasks["tasks"])[0])
        assert first_task["pid"] == 1
        assert port.kwargs == {"transport_handle": "drgn-live://127.0.0.1:22", "helper": "tasks"}

    asyncio.run(_run())


def test_run_tool_uses_already_resolved_live_session(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> tuple[ToolResponse, _CountingResolver, _FakeLiveIntrospector, int]:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool)
            port = _FakeLiveIntrospector()
            runtime = cast(
                ProviderRuntime,
                SimpleNamespace(
                    vmcore_introspector=_FakeIntrospector(),
                    live_introspector=port,
                    supported_introspection=frozenset({"live"}),
                    component_sources=SimpleNamespace(provider="local-libvirt"),
                ),
            )
            resolver = _CountingResolver(runtime)
            original_resolve = introspect_tools.resolve_live_drgn_session
            resolve_calls = 0

            async def counted_resolve(conn, ctx, session_id: str):
                nonlocal resolve_calls
                resolve_calls += 1
                return await original_resolve(conn, ctx, session_id)

            monkeypatch.setattr(introspect_tools, "current_context", lambda: _live_ctx())
            monkeypatch.setattr(introspect_tools, "resolve_live_drgn_session", counted_resolve)
            app: FastMCP = FastMCP(name="t")
            introspect_tools.register(app, pool, resolver=resolver)
            async with Client(app) as client:
                result = await client.call_tool(
                    "introspect.run",
                    {"session_id": session_id, "helper": "tasks"},
                    raise_on_error=False,
                )
            assert result.structured_content is not None
            response = ToolResponse.model_validate(result.structured_content)
            return response, resolver, port, resolve_calls

    resp, resolver, port, resolve_calls = asyncio.run(_run())

    assert resp.status != "error"
    assert resolve_calls == 1
    assert len(resolver.calls) == 1
    assert port.kwargs == {"transport_handle": "drgn-live://127.0.0.1:22", "helper": "tasks"}


def test_run_live_masks_planted_secret_in_response(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool)
            # The port is the single redaction boundary; it returns the already-masked shape.
            port = _FakeLiveIntrospector(output=_output(comm="[REDACTED]"))
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id=session_id,
                helper="tasks",
                resolver=_live_resolver(port),
            )
        report = data_mapping(resp, "report")
        tasks = json_mapping(report["tasks"])
        first_task = json_mapping(json_sequence(tasks["tasks"])[0])
        assert first_task["comm"] == "[REDACTED]"

    asyncio.run(_run())


def test_run_live_marks_transcript_sensitive(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool)
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id=session_id,
                helper="tasks",
                resolver=_live_resolver(_FakeLiveIntrospector()),
            )
        # The raw drgn-over-ssh transcript is sensitive; the response advertises that so a
        # consumer never treats the report as a substitute for the redacted-only contract.
        assert resp.data["transcript_sensitivity"] == "sensitive"

    asyncio.run(_run())


def test_run_live_unknown_helper_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool)
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id=session_id,
                helper="exec_arbitrary",
                resolver=_live_resolver(_FakeLiveIntrospector()),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_run_live_non_live_session_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool, state=DebugSessionState.DETACHED)
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id=session_id,
                helper="tasks",
                resolver=_live_resolver(_FakeLiveIntrospector()),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_run_live_non_drgn_live_session_is_config_error(migrated_url: str) -> None:
    # A live introspect.run requires an ssh transport, not gdbstub (ADR-0039 §4).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool, transport="gdbstub")
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id=session_id,
                helper="tasks",
                resolver=_live_resolver(_FakeLiveIntrospector()),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_run_live_cross_project_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool)
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(projects=("other",)),
                session_id=session_id,
                helper="tasks",
                resolver=_live_resolver(_FakeLiveIntrospector()),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_run_live_without_operator_raises(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool)
            with pytest.raises(AuthorizationError):
                await introspect_tools.introspect_run(
                    pool,
                    _live_ctx(Role.VIEWER),
                    session_id=session_id,
                    helper="tasks",
                    resolver=_live_resolver(_FakeLiveIntrospector()),
                )

    asyncio.run(_run())


def test_run_live_port_attach_failure_is_typed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool)
            err = CategorizedError("ssh dropped", category=ErrorCategory.TRANSPORT_FAILURE)
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id=session_id,
                helper="tasks",
                resolver=_live_resolver(_FakeLiveIntrospector(raises=err)),
            )
        assert resp.status == "error" and resp.error_category == "transport_failure"

    asyncio.run(_run())


def test_run_live_malformed_session_id_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await introspect_tools.introspect_run(
                pool,
                _live_ctx(),
                session_id="nope",
                helper="tasks",
                resolver=_live_resolver(_FakeLiveIntrospector()),
            )
        assert resp.status == "error" and resp.error_category == "configuration_error"

    asyncio.run(_run())


# --- introspect.script handler (ADR-0240, live arbitrary drgn) -------------------------------


def test_script_clamps_timeout_to_floor(migrated_url: str) -> None:
    # coreutils `timeout 0` disables the bound, so a 0/negative request clamps up to the floor.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool)
            port = _FakeLiveIntrospector()
            resp = await introspect_tools.introspect_script(
                pool,
                _live_ctx(),
                session_id=session_id,
                script="print(1)",
                timeout_sec=0.0,
                resolver=_live_resolver(port),
            )
        assert resp.status != "error"
        assert port.kwargs["timeout_sec"] == 1.0
        assert resp.data["output"] == "ok"
        assert resp.data["truncated"] is False

    asyncio.run(_run())


def test_script_clamps_timeout_to_ceiling(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kdive.config as config

    monkeypatch.setenv("KDIVE_LIVE_SCRIPT_MAX_TIMEOUT_SECONDS", "600")
    config.load()

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool)
            port = _FakeLiveIntrospector()
            await introspect_tools.introspect_script(
                pool,
                _live_ctx(),
                session_id=session_id,
                script="print(1)",
                timeout_sec=99999.0,
                resolver=_live_resolver(port),
            )
        assert port.kwargs["timeout_sec"] == 600.0

    asyncio.run(_run())


def test_script_threads_script_into_the_introspector(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool, transport_handle="kdive-remote-1")
            port = _FakeLiveIntrospector()
            resp = await introspect_tools.introspect_script(
                pool,
                _live_ctx(),
                session_id=session_id,
                script="print(prog['x'])",
                timeout_sec=12.0,
                resolver=_live_resolver(port),
            )
        assert resp.status != "error"
        assert port.kwargs["transport_handle"] == "kdive-remote-1"
        assert port.kwargs["script"] == "print(prog['x'])"
        assert port.kwargs["timeout_sec"] == 12.0

    asyncio.run(_run())


def test_script_seam_error_surfaces_typed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool)
            boom = CategorizedError("drgn died", category=ErrorCategory.DEBUG_ATTACH_FAILURE)
            port = _FakeLiveIntrospector(raises=boom)
            resp = await introspect_tools.introspect_script(
                pool,
                _live_ctx(),
                session_id=session_id,
                script="boom",
                timeout_sec=5.0,
                resolver=_live_resolver(port),
            )
        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.DEBUG_ATTACH_FAILURE

    asyncio.run(_run())


def test_require_introspection_rejects_when_live_script_unadvertised() -> None:
    # The descriptor gate (reused from introspect.run) refuses a provider lacking live-script.
    runtime = cast(
        ProviderRuntime,
        SimpleNamespace(
            supported_introspection=frozenset({"offline-vmcore", "live"}),  # no live-script
            component_sources=SimpleNamespace(provider="local-libvirt"),
        ),
    )
    denied = introspect_tools._require_introspection("sess-1", runtime, "live-script")
    assert denied is not None
    assert denied.error_category == ErrorCategory.CONFIGURATION_ERROR
    assert denied.data["capability"] == "introspection:live-script"


def test_script_over_size_cap_is_configuration_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool)
            port = _FakeLiveIntrospector()
            huge = "x" * (256 * 1024 + 1)
            resp = await introspect_tools.introspect_script(
                pool,
                _live_ctx(),
                session_id=session_id,
                script=huge,
                timeout_sec=5.0,
                resolver=_live_resolver(port),
            )
        assert resp.status == "error"
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR
        assert resp.data["reason"] == "script_too_large"
        assert isinstance(resp.data["script_bytes"], int)
        assert isinstance(resp.data["max_bytes"], int)
        assert port.kwargs == {}  # rejected before the seam ran

    asyncio.run(_run())


# --- live introspect missing_debuginfo warning (ADR-0322, #1064) ---------------------------------


def _patch_effective_config(config: Any) -> Any:
    from unittest.mock import patch

    async def _fake_load(conn: Any, run_id: Any, *, store_factory: Any = None) -> Any:
        return config

    return patch("kdive.kernel_config.gate.load_effective_config", _fake_load)


def test_run_live_warns_missing_debuginfo_but_still_succeeds(migrated_url: str) -> None:
    # ADR-0322: introspect.run over a debuginfo-less kernel (no uploaded vmlinux) still reports
    # `succeeded` (non-fatal) but carries a symbol-naming warning so the agent knows it was blind.
    from kdive.kernel_config.parse import KernelConfig

    cfg = KernelConfig(frozenset({"DEBUG_INFO", "DEBUG_KERNEL"}))  # no DWARF/BTF

    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool, debuginfo_ref=None)
            port = _FakeLiveIntrospector()
            with _patch_effective_config(cfg):
                return await introspect_tools.introspect_run(
                    pool,
                    _live_ctx(),
                    session_id=session_id,
                    helper="tasks",
                    resolver=_live_resolver(port),
                )

    resp = asyncio.run(_run())
    assert resp.status == "succeeded"
    warning = cast(dict[str, Any], resp.data["missing_debuginfo"])
    assert warning["reason"] == "missing_debuginfo"
    assert "DEBUG_INFO_BTF" in cast(list[str], warning["missing"])


def test_script_live_warns_missing_debuginfo_but_still_succeeds(migrated_url: str) -> None:
    from kdive.kernel_config.parse import KernelConfig

    cfg = KernelConfig(frozenset({"DEBUG_INFO", "DEBUG_KERNEL"}))

    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool, debuginfo_ref=None)
            port = _FakeLiveIntrospector()
            with _patch_effective_config(cfg):
                return await introspect_tools.introspect_script(
                    pool,
                    _live_ctx(),
                    session_id=session_id,
                    script="pass",
                    timeout_sec=5.0,
                    resolver=_live_resolver(port),
                )

    resp = asyncio.run(_run())
    assert resp.status == "succeeded"
    assert cast(dict[str, Any], resp.data["missing_debuginfo"])["reason"] == "missing_debuginfo"


def test_run_live_uploaded_vmlinux_suppresses_warning(migrated_url: str) -> None:
    # An uploaded host vmlinux (default debuginfo_ref) suppresses the warning even when the config
    # lacks in-kernel debuginfo — DWARF-via-vmlinux must keep working (ADR-0322).
    from kdive.kernel_config.parse import KernelConfig

    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_drgn_session(pool)  # default debuginfo_ref set
            port = _FakeLiveIntrospector()
            with _patch_effective_config(KernelConfig(frozenset())):
                return await introspect_tools.introspect_run(
                    pool,
                    _live_ctx(),
                    session_id=session_id,
                    helper="tasks",
                    resolver=_live_resolver(port),
                )

    resp = asyncio.run(_run())
    assert resp.status == "succeeded"
    assert "missing_debuginfo" not in resp.data
