"""Shared MCP provider-runtime resolution tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._runtime_resolution import (
    _AUTHORIZED_ALLOCATION_KIND,
    _AUTHORIZED_BOUND_RUN_KIND,
    _AUTHORIZED_RUN_KIND,
    _AUTHORIZED_SYSTEM_KIND,
    RuntimeCallback,
    with_runtime_for_allocation,
    with_runtime_for_run,
    with_runtime_for_run_target_kind,
    with_runtime_for_system,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role

type _RuntimeWrapper = Callable[
    [AsyncConnectionPool, ProviderResolver, RequestContext, str, RuntimeCallback],
    Coroutine[Any, Any, ToolResponse],
]

_OBJECT_ID = "11111111-1111-1111-1111-111111111111"
_OBJECT_UUID = UUID(_OBJECT_ID)
_RUNTIME = cast(ProviderRuntime, object())
_WRAPPERS: tuple[tuple[str, Callable[..., Coroutine[Any, Any, ToolResponse]]], ...] = (
    ("allocation", with_runtime_for_allocation),
    ("system", with_runtime_for_system),
    ("run", with_runtime_for_run),
    ("run", with_runtime_for_run_target_kind),
)
_BOUND_WRAPPERS: tuple[tuple[str, Callable[..., Coroutine[Any, Any, ToolResponse]]], ...] = (
    ("allocation", with_runtime_for_allocation),
    ("system", with_runtime_for_system),
    ("run", with_runtime_for_run),
)
_WRAPPER_SQL: tuple[tuple[str, Callable[..., Coroutine[Any, Any, ToolResponse]], str], ...] = (
    ("allocation", with_runtime_for_allocation, _AUTHORIZED_ALLOCATION_KIND),
    ("system", with_runtime_for_system, _AUTHORIZED_SYSTEM_KIND),
    ("run", with_runtime_for_run, _AUTHORIZED_BOUND_RUN_KIND),
    ("run", with_runtime_for_run_target_kind, _AUTHORIZED_RUN_KIND),
)


class _FakeCursor:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row
        self.executed: tuple[object, object] | None = None

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def execute(self, query: object, params: object) -> None:
        self.executed = (query, params)

    async def fetchone(self) -> dict[str, object] | None:
        return self._row


class _FakeConn:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.cursor_obj = _FakeCursor(row)
        self.cursor_kwargs: dict[str, object] | None = None

    def cursor(self, **kwargs: object) -> _FakeCursor:
        self.cursor_kwargs = kwargs
        return self.cursor_obj


class _ConnectionContext:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback


class _FakePool:
    def __init__(self, row: dict[str, object] | None = None) -> None:
        self.conn = _FakeConn(row)
        self.connections = 0

    def connection(self) -> _ConnectionContext:
        self.connections += 1
        return _ConnectionContext(self.conn)


class _FakeResolver:
    def __init__(self, *, error: CategorizedError | None = None) -> None:
        self.error = error
        self.calls: list[ResourceKind] = []
        self.bound_calls: list[str] = []

    def resolve(self, kind: ResourceKind) -> ProviderRuntime:
        self.calls.append(kind)
        if self.error is not None:
            raise self.error
        return _RUNTIME

    async def runtime_for_allocation(self, conn: object, allocation_id: UUID) -> ProviderRuntime:
        del conn, allocation_id
        self.bound_calls.append("allocation")
        return self.resolve(ResourceKind.LOCAL_LIBVIRT)

    async def runtime_for_system(self, conn: object, system_id: UUID) -> ProviderRuntime:
        del conn, system_id
        self.bound_calls.append("system")
        return self.resolve(ResourceKind.LOCAL_LIBVIRT)

    async def runtime_for_run(self, conn: object, run_id: UUID) -> ProviderRuntime:
        del conn, run_id
        self.bound_calls.append("run")
        return self.resolve(ResourceKind.LOCAL_LIBVIRT)


def _pool(pool: _FakePool) -> AsyncConnectionPool:
    return cast(AsyncConnectionPool, pool)


def _resolver(resolver: _FakeResolver) -> ProviderResolver:
    return cast(ProviderResolver, resolver)


def _ctx(*, project: str = "proj", role: Role = Role.OPERATOR) -> RequestContext:
    return RequestContext(
        principal="alice",
        agent_session=None,
        projects=(project,),
        roles={project: role},
    )


def _row(
    *,
    project: str = "proj",
    kind: str | None = ResourceKind.LOCAL_LIBVIRT.value,
    name: str = "host-a",
    system_id: UUID | None = _OBJECT_UUID,
):
    return {"project": project, "kind": kind, "name": name, "system_id": system_id}


async def _call(
    wrapper: Callable[..., Coroutine[Any, Any, ToolResponse]],
    pool: _FakePool,
    resolver: _FakeResolver,
    object_id: str,
    ctx: RequestContext,
) -> ToolResponse:
    return await wrapper(
        _pool(pool),
        _resolver(resolver),
        ctx,
        object_id,
        _success_response,
        required_role=Role.OPERATOR,
    )


@pytest.mark.parametrize(("kind", "wrapper"), _WRAPPERS)
def test_runtime_wrapper_maps_malformed_id_to_failure_response(
    kind: str, wrapper: _RuntimeWrapper
) -> None:
    del kind
    pool = _FakePool()
    resolver = _FakeResolver()

    result = asyncio.run(_call(wrapper, pool, resolver, "not-a-uuid", _ctx()))

    assert result.object_id == "not-a-uuid"
    assert result.status == "error"
    assert result.error_category == "configuration_error"
    assert pool.connections == 0
    assert resolver.calls == []


@pytest.mark.parametrize(("kind", "wrapper"), _WRAPPERS)
def test_runtime_wrapper_maps_categorized_error_to_failure_response(
    kind: str, wrapper: _RuntimeWrapper
) -> None:
    del kind
    error = CategorizedError(
        "runtime unavailable",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={
            "resource_kind": "local_libvirt",
            "retryable": False,
            "nested": {"not": "surfaced"},
        },
    )
    pool = _FakePool(_row())
    resolver = _FakeResolver(error=error)

    result = asyncio.run(_call(wrapper, pool, resolver, _OBJECT_ID, _ctx()))

    assert result.object_id == _OBJECT_ID
    assert result.status == "error"
    assert result.error_category == "missing_dependency"
    assert result.data == {"resource_kind": "local_libvirt", "retryable": False}
    assert pool.connections == 1
    assert resolver.calls == [ResourceKind.LOCAL_LIBVIRT]


@pytest.mark.parametrize(("kind", "wrapper"), _WRAPPERS)
def test_runtime_wrapper_preserves_absent_object_not_found_response(
    kind: str, wrapper: _RuntimeWrapper
) -> None:
    pool = _FakePool()
    resolver = _FakeResolver()

    result = asyncio.run(_call(wrapper, pool, resolver, _OBJECT_ID, _ctx()))

    assert result.object_id == _OBJECT_ID
    assert result.status == "error"
    assert result.error_category == "not_found"
    assert result.data == {"object_kind": kind, "object_id": _OBJECT_ID}
    assert pool.connections == 1
    assert resolver.calls == []


@pytest.mark.parametrize(("kind", "wrapper"), _WRAPPERS)
def test_runtime_wrapper_authorizes_project_before_resolving_runtime(
    kind: str, wrapper: _RuntimeWrapper
) -> None:
    pool = _FakePool(_row(project="other"))
    resolver = _FakeResolver()

    result = asyncio.run(_call(wrapper, pool, resolver, _OBJECT_ID, _ctx(project="proj")))

    assert result.object_id == _OBJECT_ID
    assert result.status == "error"
    assert result.error_category == "not_found"
    assert result.data == {"object_kind": kind, "object_id": _OBJECT_ID}
    assert resolver.calls == []


@pytest.mark.parametrize(("kind", "wrapper", "sql"), _WRAPPER_SQL)
def test_runtime_wrapper_resolves_and_invokes_callback_on_authorized_object(
    kind: str, wrapper: Callable[..., Coroutine[Any, Any, ToolResponse]], sql: str
) -> None:
    del kind
    pool = _FakePool(_row())
    resolver = _FakeResolver()

    result = asyncio.run(_call(wrapper, pool, resolver, _OBJECT_ID, _ctx()))

    assert result.object_id == _OBJECT_ID
    assert result.status == "succeeded"
    assert result.error_category is None
    assert resolver.calls == [ResourceKind.LOCAL_LIBVIRT]
    cursor = pool.conn.cursor_obj
    assert cursor.executed is not None
    executed_sql, executed_params = cursor.executed
    assert executed_sql == sql
    assert executed_params == (UUID(_OBJECT_ID),)
    assert pool.conn.cursor_kwargs == {"row_factory": dict_row}


@pytest.mark.parametrize(("kind", "wrapper"), _BOUND_WRAPPERS)
def test_bound_runtime_wrapper_rebinds_runtime_to_resource_name(
    kind: str, wrapper: Callable[..., Coroutine[Any, Any, ToolResponse]]
) -> None:
    del kind
    pool = _FakePool(_row(name="host-bound"))
    runtime = _BindableRuntime()
    resolver = ProviderResolver(cast(dict, {ResourceKind.LOCAL_LIBVIRT: runtime}))

    async def _callback(bound: ProviderRuntime) -> ToolResponse:
        assert isinstance(bound, _BindableRuntime)
        assert bound.bound_to == "host-bound"
        return ToolResponse.success(_OBJECT_ID, "succeeded")

    result = asyncio.run(
        wrapper(
            _pool(pool),
            resolver,
            _ctx(),
            _OBJECT_ID,
            _callback,
            required_role=Role.OPERATOR,
        )
    )

    assert result.status == "succeeded"
    assert runtime.bound_to is None


def test_bound_run_wrapper_preserves_unbound_run_configuration_error() -> None:
    pool = _FakePool(_row(system_id=None))
    resolver = _FakeResolver()

    result = asyncio.run(_call(with_runtime_for_run, pool, resolver, _OBJECT_ID, _ctx()))

    assert result.object_id == _OBJECT_ID
    assert result.status == "error"
    assert result.error_category == "configuration_error"
    assert result.data == {"reason": "run_unbound"}
    assert resolver.calls == []
    assert resolver.bound_calls == []


async def _success_response(runtime: ProviderRuntime) -> ToolResponse:
    assert runtime is _RUNTIME
    return ToolResponse.success(_OBJECT_ID, "succeeded")


class _BindableRuntime:
    def __init__(self, bound_to: str | None = None) -> None:
        self.bound_to = bound_to

    def for_resource(self, resource_name: str) -> _BindableRuntime:
        return _BindableRuntime(resource_name)
