"""Fault-inject discovery: one synthetic resource row carrying the fault-engine keys."""

from __future__ import annotations

from kdive.domain.capacity.state import ResourceStatus
from kdive.domain.catalog.resource_capabilities import (
    CONCURRENT_ALLOCATION_CAP_KEY,
    MEMORY_MB_KEY,
    VCPUS_KEY,
)
from kdive.domain.catalog.resources import ResourceKind
from kdive.providers.fault_inject.capabilities import (
    FAULT_RATE_KEY,
    MAX_LATENCY_S_KEY,
    SECRET_REF_KEY,
    SEED_KEY,
)
from kdive.providers.fault_inject.discovery import FaultInjectDiscovery


def test_list_resources_returns_one_available_fault_inject_row() -> None:
    discovery = FaultInjectDiscovery.from_env()

    records = discovery.list_resources()

    assert len(records) == 1
    (record,) = records
    assert record["kind"] is ResourceKind.FAULT_INJECT
    assert record["resource_id"] == discovery.host_uri
    assert record["status"] is ResourceStatus.AVAILABLE


def test_capabilities_carry_the_fault_engine_keys_and_the_allocation_cap() -> None:
    discovery = FaultInjectDiscovery.from_env()

    (record,) = discovery.list_resources()
    capabilities = record["capabilities"]

    # No new columns: seed / per-plane fault_rate / per-plane max_latency_s / secret_ref
    # ride the existing capabilities jsonb, set by discovery (ADR-0072).
    assert SEED_KEY in capabilities
    assert MAX_LATENCY_S_KEY in capabilities
    assert capabilities[SECRET_REF_KEY]
    assert CONCURRENT_ALLOCATION_CAP_KEY in capabilities


def test_default_fault_rate_is_empty_so_the_happy_path_draws_no_fault() -> None:
    discovery = FaultInjectDiscovery.from_env()

    (record,) = discovery.list_resources()

    # M1.5 issue 2 is happy-path only: discovery writes inert config (fault_rate empty,
    # max_latency_s empty) the seeded engine (issue 3) overrides to force faults.
    assert record["capabilities"][FAULT_RATE_KEY] == {}
    assert record["capabilities"][MAX_LATENCY_S_KEY] == {}


def test_discovery_no_longer_hardcodes_billable_sizing() -> None:
    # Task 2.2 (#393): sizing (vcpus/memory_mb) now comes from the systems.toml config overlay
    # (reconcile_resources), NOT a hardcoded discovery dict. Discovery only describes the
    # synthetic engine config + cap; the #385 fix is the overlay supplying sizing onto the row.
    discovery = FaultInjectDiscovery.from_env()

    (record,) = discovery.list_resources()
    capabilities = record["capabilities"]

    assert VCPUS_KEY not in capabilities
    assert MEMORY_MB_KEY not in capabilities


def test_seed_and_cap_are_read_from_the_environment() -> None:
    discovery = FaultInjectDiscovery(
        host_uri="fault-inject://test",
        concurrent_allocation_cap=4,
        seed=12345,
        fault_rate={"provision": 0.5},
        max_latency_s={"provision": 9.0},
        secret_ref="fault-inject/sentinel",  # pragma: allowlist secret - ref, not a value
    )

    assert discovery.host_uri == "fault-inject://test"
    (record,) = discovery.list_resources()
    assert record["resource_id"] == "fault-inject://test"
    capabilities = record["capabilities"]

    assert capabilities[SEED_KEY] == 12345
    assert capabilities[CONCURRENT_ALLOCATION_CAP_KEY] == 4
    assert capabilities[FAULT_RATE_KEY] == {"provision": 0.5}
    assert capabilities[MAX_LATENCY_S_KEY] == {"provision": 9.0}


def test_from_env_populates_fields_from_setting_defaults() -> None:
    discovery = FaultInjectDiscovery.from_env()

    assert discovery.host_uri == "fault-inject://local"
    assert discovery.concurrent_allocation_cap == 1
    assert discovery.seed == 0
    assert discovery.secret_ref == "fault-inject/console-sentinel"  # pragma: allowlist secret


def test_capabilities_describe_the_synthetic_gdbstub_engine() -> None:
    discovery = FaultInjectDiscovery.from_env()

    (record,) = discovery.list_resources()
    capabilities = record["capabilities"]

    assert capabilities["arch"] == "synthetic"
    assert capabilities["transports"] == ["gdbstub"]
