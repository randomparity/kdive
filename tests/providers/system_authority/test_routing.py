"""Tests for authority route selection from fixed provider configuration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kdive.domain.catalog.resources import ResourceKind
from kdive.providers.system_authority import routing


def test_authority_route_uses_only_the_matching_provider_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routing, "local_authority_instance_for_resource", lambda _: "local-a")
    monkeypatch.setattr(
        routing,
        "remote_config_for_resource",
        lambda _: SimpleNamespace(authority=SimpleNamespace(authority_instance="remote-a")),
    )

    assert routing.authority_instance_for_resource(ResourceKind.LOCAL_LIBVIRT, "local") == "local-a"
    assert (
        routing.authority_instance_for_resource(ResourceKind.REMOTE_LIBVIRT, "remote") == "remote-a"
    )
    assert routing.authority_instance_for_resource(ResourceKind.FAULT_INJECT, "fault") is None


def test_admission_routes_union_the_route_and_the_local_relocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routing, "local_authority_instance_for_resource", lambda _: None)
    monkeypatch.setattr(routing, "local_relocated_authority_for_resource", lambda _: None)
    assert (
        routing.authority_admission_routes_for_resource(ResourceKind.LOCAL_LIBVIRT, "loc")
        == frozenset()
    )

    monkeypatch.setattr(routing, "local_authority_instance_for_resource", lambda _: "route-a")
    assert routing.authority_admission_routes_for_resource(
        ResourceKind.LOCAL_LIBVIRT, "loc"
    ) == frozenset({"route-a"})

    monkeypatch.setattr(routing, "local_authority_instance_for_resource", lambda _: None)
    monkeypatch.setattr(routing, "local_relocated_authority_for_resource", lambda _: "move-a")
    assert routing.authority_admission_routes_for_resource(
        ResourceKind.LOCAL_LIBVIRT, "loc"
    ) == frozenset({"move-a"})
    assert (
        routing.authority_admission_routes_for_resource(ResourceKind.FAULT_INJECT, "f")
        == frozenset()
    )
