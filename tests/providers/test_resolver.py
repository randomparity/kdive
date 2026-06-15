"""Unit tests for the per-kind ProviderResolver (ADR-0071)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import ResourceKind
from kdive.providers.core.resolver import ProviderResolver


class _Runtime:
    def __init__(self, label: str) -> None:
        self.label = label
        self.registered: list[object] = []

    async def register_discovery(self, pool: object) -> None:
        self.registered.append(pool)


def _resolver(*kinds: ResourceKind) -> tuple[ProviderResolver, dict[ResourceKind, _Runtime]]:
    runtimes = {k: _Runtime(k.value) for k in kinds}
    return ProviderResolver(cast(dict, runtimes)), runtimes


def test_resolve_returns_the_registered_runtime() -> None:
    resolver, runtimes = _resolver(ResourceKind.LOCAL_LIBVIRT)
    assert resolver.resolve(ResourceKind.LOCAL_LIBVIRT) is runtimes[ResourceKind.LOCAL_LIBVIRT]


def test_resolve_unknown_kind_fails_closed_with_configuration_error() -> None:
    resolver, _ = _resolver(ResourceKind.LOCAL_LIBVIRT)
    with pytest.raises(CategorizedError) as exc:
        resolver.resolve(ResourceKind.FAULT_INJECT)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "fault-inject" in str(exc.value)


def test_registered_kinds_reflects_the_map() -> None:
    resolver, _ = _resolver(ResourceKind.LOCAL_LIBVIRT)
    assert resolver.registered_kinds() == frozenset({ResourceKind.LOCAL_LIBVIRT})


def test_empty_resolver_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ProviderResolver({})


def test_register_all_discovery_fans_out_over_every_runtime() -> None:
    resolver, runtimes = _resolver(ResourceKind.LOCAL_LIBVIRT)
    pool = cast(AsyncConnectionPool, object())
    asyncio.run(resolver.register_all_discovery(pool))
    assert runtimes[ResourceKind.LOCAL_LIBVIRT].registered == [pool]


class _FailingRuntime(_Runtime):
    async def register_discovery(self, pool: object) -> None:
        raise RuntimeError("no local libvirtd on this host")


def test_register_all_discovery_isolates_one_runtimes_failure() -> None:
    """A worker-only host: local discovery fails, the remote runtime still registers."""
    failing = _FailingRuntime("local-libvirt")
    healthy = _Runtime("remote-libvirt")
    resolver = ProviderResolver(
        cast(
            dict,
            {ResourceKind.LOCAL_LIBVIRT: failing, ResourceKind.REMOTE_LIBVIRT: healthy},
        )
    )
    pool = cast(AsyncConnectionPool, object())
    with pytest.raises(RuntimeError):
        asyncio.run(resolver.register_all_discovery(pool))
    assert healthy.registered == [pool]


class _CursorContext:
    def __init__(self, row: dict[str, str] | None) -> None:
        self.row = row
        self.executed: tuple[object, tuple[UUID, ...]] | None = None

    async def __aenter__(self) -> _CursorContext:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def execute(self, sql: object, params: tuple[UUID, ...]) -> None:
        self.executed = (sql, params)

    async def fetchone(self) -> dict[str, str] | None:
        return self.row


class _Conn:
    def __init__(self, row: dict[str, str] | None) -> None:
        self.cursor_context = _CursorContext(row)

    def cursor(self, *, row_factory: object) -> _CursorContext:
        del row_factory
        return self.cursor_context


_ABSENT_OBJECT_ID = UUID("11111111-1111-1111-1111-111111111111")
type _RuntimeLookup = Callable[[ProviderResolver, _Conn, UUID], Coroutine[Any, Any, Any]]


@pytest.mark.parametrize(
    ("object_kind", "resolve"),
    (
        ("allocation", ProviderResolver.runtime_for_allocation),
        ("system", ProviderResolver.runtime_for_system),
        ("run", ProviderResolver.runtime_for_run),
        ("session", ProviderResolver.runtime_for_session),
        ("session", ProviderResolver.binding_for_session),
    ),
)
def test_runtime_lookup_absent_object_fails_with_not_found(
    object_kind: str,
    resolve: _RuntimeLookup,
) -> None:
    resolver, _ = _resolver(ResourceKind.LOCAL_LIBVIRT)
    conn = _Conn(None)

    with pytest.raises(CategorizedError) as exc:
        asyncio.run(resolve(resolver, conn, _ABSENT_OBJECT_ID))

    assert exc.value.category is ErrorCategory.NOT_FOUND
    assert exc.value.details == {
        "object_kind": object_kind,
        "object_id": str(_ABSENT_OBJECT_ID),
    }
