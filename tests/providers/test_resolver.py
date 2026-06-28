"""Unit tests for the per-kind ProviderResolver (ADR-0071)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.core.resolver import (
    _KIND_FOR_ALLOCATION,
    _KIND_FOR_RUN,
    _KIND_FOR_SESSION,
    _KIND_FOR_SYSTEM,
    ProviderResolver,
)
from kdive.serialization import safe_error_details


class _Runtime:
    def __init__(self, label: str) -> None:
        self.label = label
        self.registered: list[object] = []
        self.bound_to: str | None = None

    async def register_discovery(self, pool: object) -> None:
        self.registered.append(pool)

    def for_resource(self, resource_name: str) -> _Runtime:
        self.bound_to = resource_name
        return self


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
    # The fail-closed error carries the offending kind and the set that *is* composed, under the
    # allowlisted "available" key so safe_error_details preserves it (see the serialization test
    # below). This mirrors assert_kind_composed (ADR-0269) so both fail-closed paths agree.
    assert exc.value.details == {
        "kind": "fault-inject",
        "available": ["local-libvirt"],
    }


def test_resolve_failure_details_survive_safe_error_details() -> None:
    """The composed-kinds list reaches the caller after redaction filtering (#885).

    ``safe_error_details`` (the error-envelope redaction boundary) only preserves lists under
    keys in ``_ENUMERATION_KEYS`` (``{"accepted_values", "available"}``). The fail-closed
    ``resolve()`` path previously emitted the list under ``"registered"``, which is not
    allowlisted, so the list was silently dropped and a runtime resolution failure returned an
    envelope that did not enumerate the valid kinds. Emitting under ``"available"`` lets the
    list survive, mirroring ``test_rejection_envelope_enumerates_composed_kinds``.
    """
    resolver, _ = _resolver(ResourceKind.LOCAL_LIBVIRT)
    with pytest.raises(CategorizedError) as exc:
        resolver.resolve(ResourceKind.FAULT_INJECT)
    safe = safe_error_details(exc.value.details)
    assert safe.get("available") == ["local-libvirt"]
    assert safe.get("kind") == "fault-inject"


def test_registered_kinds_reflects_the_map() -> None:
    resolver, _ = _resolver(ResourceKind.LOCAL_LIBVIRT)
    assert resolver.registered_kinds() == frozenset({ResourceKind.LOCAL_LIBVIRT})


def test_empty_resolver_is_allowed_and_fails_closed_at_resolve(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # ADR-0131: with local-libvirt gateable, a fully-disabled deployment yields an empty
    # runtime map. That must not crash startup with a ValueError — it fails closed at
    # resolution instead, and discovery registration over an empty set is a no-op. The
    # constructor warns so the request tiers surface a zero-provider deploy at startup.
    with caplog.at_level("WARNING", logger="kdive.providers.core.resolver"):
        resolver = ProviderResolver({})
    assert any("no registered runtimes" in record.message for record in caplog.records)
    assert resolver.registered_kinds() == frozenset()
    asyncio.run(resolver.register_all_discovery(cast(AsyncConnectionPool, object())))
    with pytest.raises(CategorizedError) as exc:
        resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


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

# The exact query each lookup must issue. A method wiring the wrong (or a None) SQL constant
# would resolve the wrong object graph in production; the fake conn returns the same row
# regardless, so the SQL identity is asserted directly.
_SQL_FOR_KIND = {
    "allocation": _KIND_FOR_ALLOCATION,
    "system": _KIND_FOR_SYSTEM,
    "run": _KIND_FOR_RUN,
    "session": _KIND_FOR_SESSION,
}


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
    assert str(exc.value) == f"{object_kind} {_ABSENT_OBJECT_ID} was not found"
    assert exc.value.details == {
        "object_kind": object_kind,
        "object_id": str(_ABSENT_OBJECT_ID),
    }
    # The lookup must issue its own kind's query, parameterized by the object id.
    assert conn.cursor_context.executed == (
        _SQL_FOR_KIND[object_kind],
        (_ABSENT_OBJECT_ID,),
    )


def test_runtime_for_system_binds_to_resource_name() -> None:
    # ADR-0187: runtime_for_system resolves (kind, name) and returns the runtime bound to the
    # System's Resource name, so a per-op call reaches the allocated host.
    resolver, runtimes = _resolver(ResourceKind.REMOTE_LIBVIRT)
    conn = cast(AsyncConnection, _Conn({"kind": "remote-libvirt", "name": "host-b"}))
    bound = asyncio.run(resolver.runtime_for_system(conn, _ABSENT_OBJECT_ID))
    assert bound is runtimes[ResourceKind.REMOTE_LIBVIRT]
    assert runtimes[ResourceKind.REMOTE_LIBVIRT].bound_to == "host-b"


def test_binding_for_system_returns_binding_with_kind_and_bound_runtime() -> None:
    resolver, runtimes = _resolver(ResourceKind.REMOTE_LIBVIRT)
    conn = cast(AsyncConnection, _Conn({"kind": "remote-libvirt", "name": "host-c"}))
    binding = asyncio.run(resolver.binding_for_system(conn, _ABSENT_OBJECT_ID))
    assert binding.kind is ResourceKind.REMOTE_LIBVIRT
    assert binding.runtime is runtimes[ResourceKind.REMOTE_LIBVIRT]
    assert runtimes[ResourceKind.REMOTE_LIBVIRT].bound_to == "host-c"


def test_binding_for_run_returns_binding_with_kind_and_bound_runtime() -> None:
    resolver, runtimes = _resolver(ResourceKind.LOCAL_LIBVIRT)
    conn = cast(AsyncConnection, _Conn({"kind": "local-libvirt", "name": "host-d"}))
    binding = asyncio.run(resolver.binding_for_run(conn, _ABSENT_OBJECT_ID))
    assert binding.kind is ResourceKind.LOCAL_LIBVIRT
    assert binding.runtime is runtimes[ResourceKind.LOCAL_LIBVIRT]
    assert runtimes[ResourceKind.LOCAL_LIBVIRT].bound_to == "host-d"


@pytest.mark.parametrize(
    ("object_kind", "resolve"),
    (
        ("system", ProviderResolver.binding_for_system),
        ("run", ProviderResolver.binding_for_run),
    ),
)
def test_binding_lookup_absent_object_fails_with_not_found(
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
    assert conn.cursor_context.executed == (
        _SQL_FOR_KIND[object_kind],
        (_ABSENT_OBJECT_ID,),
    )
