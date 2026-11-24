"""Direct structural pin for the runs.* job-handler DI container (ADR-0268, #866)."""

from __future__ import annotations

import dataclasses
from typing import cast

import pytest
from pydantic import SecretStr

from kdive.jobs.handlers.runs.ports import RunHandlerPorts
from kdive.providers.core.resolver import ProviderResolver


def _ports() -> RunHandlerPorts:
    return RunHandlerPorts(
        resolver=cast(ProviderResolver, object()),
        incarnation_credential=SecretStr("worker-test-incarnation-credential"),
    )


def test_exposes_the_two_handler_dependencies() -> None:
    fields = {f.name for f in dataclasses.fields(RunHandlerPorts)}
    assert fields == {
        "resolver",
        "incarnation_credential",
    }


def test_is_frozen_and_slotted() -> None:
    ports = _ports()
    assert RunHandlerPorts.__dataclass_params__.frozen is True
    assert not hasattr(ports, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ports.resolver = object()  # ty: ignore[invalid-assignment]


def test_binds_each_dependency_by_name() -> None:
    resolver = object()
    credential = SecretStr("worker-test-incarnation-credential")
    ports = RunHandlerPorts(
        resolver=cast(ProviderResolver, resolver),
        incarnation_credential=credential,
    )
    assert ports.resolver is resolver
    assert ports.incarnation_credential is credential
