"""Unit cases for ``kdive.services.external_boot.route_admission`` (ADR-0752)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.core.resolver import ProviderBinding
from kdive.services.external_boot import route_admission
from kdive.services.external_boot.route_admission import admit_authority_route


def _binding(server_route: str | None, name: str | None = "loc") -> ProviderBinding:
    runtime = SimpleNamespace(authority=SimpleNamespace(authority_instance=server_route))
    runtime = SimpleNamespace(authority=None) if server_route is None else runtime
    return ProviderBinding(
        kind=ResourceKind.LOCAL_LIBVIRT, runtime=cast(Any, runtime), resource_name=name
    )


def _admit(
    monkeypatch: pytest.MonkeyPatch,
    binding: ProviderBinding,
    *,
    owner: str | None = None,
    routes: frozenset[str] = frozenset(),
) -> str | None:
    async def fake_owner(_conn: object, _system_id: object) -> object:
        return None if owner is None else SimpleNamespace(authority_instance=owner)

    monkeypatch.setattr(route_admission, "authority_system_binding", fake_owner)
    monkeypatch.setattr(
        route_admission, "authority_admission_routes_for_resource", lambda _k, _n: routes
    )
    return asyncio.run(admit_authority_route(cast(Any, None), uuid4(), binding))


def _refused(monkeypatch: pytest.MonkeyPatch, binding: ProviderBinding, **kwargs: Any) -> str:
    with pytest.raises(CategorizedError) as exc:
        _admit(monkeypatch, binding, **kwargs)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    return str((exc.value.details or {})["reason"])


def test_no_server_route_keeps_the_ordinary_path(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _admit(monkeypatch, _binding(None), owner="other") is None


def test_a_matching_resource_route_or_relocation_admits(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _admit(monkeypatch, _binding("a"), routes=frozenset({"a"})) == "a"


def test_an_unrouted_resource_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _refused(monkeypatch, _binding("a")) == "authority_resource_route_mismatch"
    assert (
        _refused(monkeypatch, _binding("a"), routes=frozenset({"b"}))
        == "authority_resource_route_mismatch"
    )


def test_a_nameless_resource_is_refused_without_a_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _binding("a", name=None)
    assert (
        _refused(monkeypatch, binding, routes=frozenset({"a"}))
        == "authority_resource_route_mismatch"
    )


def test_a_recorded_owner_decides_before_the_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _admit(monkeypatch, _binding("a"), owner="a") == "a"
    assert (
        _refused(monkeypatch, _binding("a"), owner="b", routes=frozenset({"a"}))
        == "authority_system_binding_mismatch"
    )
