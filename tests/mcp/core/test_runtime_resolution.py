"""Shared MCP provider-runtime resolution tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from types import TracebackType
from typing import Any, cast

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._runtime_resolution import (
    RuntimeCallback,
    with_runtime_for_allocation,
    with_runtime_for_run,
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
_RUNTIME = cast(ProviderRuntime, object())
_WRAPPERS: tuple[tuple[str, Callable[..., Coroutine[Any, Any, ToolResponse]]], ...] = (
    ("allocation", with_runtime_for_allocation),
    ("system", with_runtime_for_system),
    ("run", with_runtime_for_run),
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

    def cursor(self, **kwargs: object) -> _FakeCursor:
        del kwargs
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

    def resolve(self, kind: ResourceKind) -> ProviderRuntime:
        self.calls.append(kind)
        if self.error is not None:
            raise self.error
        return _RUNTIME


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


def _row(*, project: str = "proj", kind: str | None = ResourceKind.LOCAL_LIBVIRT.value):
    return {"project": project, "kind": kind}


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


async def _success_response(runtime: ProviderRuntime) -> ToolResponse:
    assert runtime is _RUNTIME
    return ToolResponse.success(_OBJECT_ID, "succeeded")
