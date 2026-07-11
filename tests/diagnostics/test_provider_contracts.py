"""Pin the provider-owned diagnostic contribution contracts."""

from __future__ import annotations

import dataclasses

import pytest

from kdive.diagnostics.provider_contracts import (
    DiagnosticProviderContribution,
    WorkerVantageDescriptor,
)


def test_worker_vantage_descriptor_fields() -> None:
    d = WorkerVantageDescriptor(id="check-1", provider="local-libvirt")
    assert d.id == "check-1"
    assert d.provider == "local-libvirt"
    assert {f.name for f in dataclasses.fields(d)} == {"id", "provider"}


def test_worker_vantage_descriptor_is_frozen() -> None:
    d = WorkerVantageDescriptor(id="c", provider="p")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.id = "other"  # ty: ignore[invalid-assignment]


def test_provider_contribution_field_set() -> None:
    fields = {f.name for f in dataclasses.fields(DiagnosticProviderContribution)}
    assert fields == {
        "provider",
        "enabled",
        "checks",
        "unavailable_worker_checks",
        "worker_checks",
        "egress_checks",
    }


def test_provider_contribution_is_frozen() -> None:
    contribution = DiagnosticProviderContribution(
        provider="p",
        enabled=lambda: True,
        checks=lambda: [],
        unavailable_worker_checks=lambda: [],
        worker_checks=lambda: [],
    )
    assert contribution.enabled() is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        contribution.provider = "other"  # ty: ignore[invalid-assignment]
