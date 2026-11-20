"""Fault-inject capability keys are validated at binding, not ignored when falsy (#3747)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

import pytest
from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.assembly import composition
from kdive.providers.fault_inject.capabilities import (
    FAULT_RATE_KEY,
    HOTPLUG_BEHAVIOUR_KEY,
    MAX_LATENCY_S_KEY,
    SEED_KEY,
    validate_capabilities,
)
from tests.providers.fault_inject.test_capability_wiring import _Conn

_SYSTEM_ID = UUID("33333333-3333-3333-3333-333333333333")

MALFORMED: list[dict[str, Any]] = [
    *({key: value} for key in (FAULT_RATE_KEY, MAX_LATENCY_S_KEY) for value in (0, False, "", [])),
    *({key: "x"} for key in (FAULT_RATE_KEY, MAX_LATENCY_S_KEY)),
    {FAULT_RATE_KEY: {"provision": 2.0}},
    {MAX_LATENCY_S_KEY: {"provision": -1.0}},
    {FAULT_RATE_KEY: {"provision": "high"}},
    {SEED_KEY: "7"},
    {SEED_KEY: True},
    {SEED_KEY: None},
    {HOTPLUG_BEHAVIOUR_KEY: "explode"},
    {HOTPLUG_BEHAVIOUR_KEY: 0},
]
WELL_FORMED: list[dict[str, Any]] = [
    {},
    {SEED_KEY: 7},
    {FAULT_RATE_KEY: {}, MAX_LATENCY_S_KEY: {}},
    {FAULT_RATE_KEY: None},
    {FAULT_RATE_KEY: {"provision": 0.5}, MAX_LATENCY_S_KEY: {"provision": 2}, SEED_KEY: 3},
    {HOTPLUG_BEHAVIOUR_KEY: "slow"},
]


@pytest.mark.parametrize("capabilities", MALFORMED)
def test_validator_rejects_each_malformed_key(capabilities: dict[str, Any]) -> None:
    (key,) = capabilities
    with pytest.raises(CategorizedError, match=key) as raised:
        validate_capabilities(capabilities)
    assert raised.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert repr(capabilities[key]) in str(raised.value)


@pytest.mark.parametrize("capabilities", WELL_FORMED)
def test_validator_accepts_a_well_formed_row(capabilities: dict[str, Any]) -> None:
    validate_capabilities(capabilities)


@pytest.mark.parametrize("capabilities", MALFORMED)
def test_binding_fails_closed_on_each_malformed_key(capabilities: dict[str, Any]) -> None:
    resolver = composition.ProviderComposition().build_provider_resolver(
        enable_fault_inject=True, enable_local_libvirt=False
    )
    conn = cast(AsyncConnection, _Conn(capabilities))

    with pytest.raises(CategorizedError) as raised:
        asyncio.run(resolver.binding_for_system(conn, _SYSTEM_ID))
    assert raised.value.category is ErrorCategory.CONFIGURATION_ERROR


@pytest.mark.parametrize("capabilities", WELL_FORMED)
def test_binding_accepts_a_well_formed_row(capabilities: dict[str, Any]) -> None:
    resolver = composition.ProviderComposition().build_provider_resolver(
        enable_fault_inject=True, enable_local_libvirt=False
    )
    conn = cast(AsyncConnection, _Conn(capabilities))

    assert asyncio.run(resolver.binding_for_system(conn, _SYSTEM_ID)).runtime is not None
