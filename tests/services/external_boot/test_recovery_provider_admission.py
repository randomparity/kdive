"""Server-side admission of a recovery orphan by fixed authority route (ADR-0615, #3437)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from kdive.domain.catalog.resources import ResourceKind
from kdive.mcp.tools.external_boot.recovery_requests import _recovery_provider_configured

_ROUTE = "authority-a"


def _binding(kind: ResourceKind, *, route: str | None = _ROUTE, port: object | None = None) -> Any:
    authority = None if route is None else SimpleNamespace(authority_instance=route)
    runtime = SimpleNamespace(authority=authority, external_boot_recovery_objects=port)
    return SimpleNamespace(kind=kind, runtime=runtime)


def _rows(kind: ResourceKind, *routes: str) -> list[dict[str, object]]:
    return [{"provider_kind": kind.value, "authority_instance": r} for r in routes]


@pytest.mark.parametrize("kind", [ResourceKind.LOCAL_LIBVIRT, ResourceKind.REMOTE_LIBVIRT])
def test_a_row_published_by_the_servers_fixed_route_is_admitted(kind: ResourceKind) -> None:
    assert _recovery_provider_configured(_binding(kind), _rows(kind, _ROUTE, _ROUTE))


@pytest.mark.parametrize("kind", [ResourceKind.LOCAL_LIBVIRT, ResourceKind.REMOTE_LIBVIRT])
def test_a_row_from_another_authority_instance_is_refused(kind: ResourceKind) -> None:
    assert not _recovery_provider_configured(_binding(kind), _rows(kind, _ROUTE, "authority-b"))


def test_a_remote_server_without_a_fixed_route_refuses() -> None:
    kind = ResourceKind.REMOTE_LIBVIRT
    assert not _recovery_provider_configured(_binding(kind, route=None), _rows(kind, _ROUTE))


def test_a_row_of_another_provider_kind_is_refused_even_on_the_route() -> None:
    rows = _rows(ResourceKind.LOCAL_LIBVIRT, _ROUTE)
    assert not _recovery_provider_configured(_binding(ResourceKind.REMOTE_LIBVIRT), rows)


def test_a_composed_recovery_port_admits_without_a_route() -> None:
    kind = ResourceKind.FAULT_INJECT
    binding = _binding(kind, route=None, port=cast(Any, object()))
    assert _recovery_provider_configured(binding, _rows(kind, "anything"))
