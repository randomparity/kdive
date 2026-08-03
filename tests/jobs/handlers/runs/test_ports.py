"""Direct structural pin for the runs.* job-handler DI container (ADR-0268, #866)."""

from __future__ import annotations

import dataclasses
from typing import cast

import pytest
from pydantic import SecretStr

from kdive.jobs.handlers.runs.ports import RunHandlerPorts
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore


def _ports() -> RunHandlerPorts:
    return RunHandlerPorts(
        resolver=cast(ProviderResolver, object()),
        incarnation_credential=SecretStr("worker-test-incarnation-credential"),
        secret_registry=cast(SecretRegistry, object()),
        artifact_store=cast(ObjectStore, object()),
    )


def test_exposes_the_four_shared_dependencies() -> None:
    fields = {f.name for f in dataclasses.fields(RunHandlerPorts)}
    assert fields == {
        "resolver",
        "incarnation_credential",
        "secret_registry",
        "artifact_store",
    }


def test_is_frozen_and_slotted() -> None:
    ports = _ports()
    assert RunHandlerPorts.__dataclass_params__.frozen is True
    assert not hasattr(ports, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ports.resolver = object()  # type: ignore[misc]


def test_binds_each_dependency_by_name() -> None:
    resolver, registry, store = object(), object(), object()
    credential = SecretStr("worker-test-incarnation-credential")
    ports = RunHandlerPorts(
        resolver=cast(ProviderResolver, resolver),
        incarnation_credential=credential,
        secret_registry=cast(SecretRegistry, registry),
        artifact_store=cast(ObjectStore, store),
    )
    assert ports.resolver is resolver
    assert ports.incarnation_credential is credential
    assert ports.secret_registry is registry
    assert ports.artifact_store is store
