"""Shared resource response envelopes for catalog reads and operator host mutations."""

from __future__ import annotations

from kdive.domain.catalog.resource_capabilities import (
    CONCURRENT_ALLOCATION_CAP_KEY,
    MEMORY_MB_KEY,
    VCPUS_KEY,
)
from kdive.domain.catalog.resources import Resource
from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.serialization import JsonValue

# Numeric size ceilings emitted as native JSON ints (ADR-0263); a stored value that is not
# coercible to int is dropped, matching the prior `is not None` skip for an absent key.
_INT_CAP_KEYS = (VCPUS_KEY, MEMORY_MB_KEY, CONCURRENT_ALLOCATION_CAP_KEY)


def resource_config_error(object_id: str) -> ToolResponse:
    return ToolResponse.failure(object_id, ErrorCategory.CONFIGURATION_ERROR)


def resource_capability_data(resource: Resource) -> dict[str, JsonValue]:
    """Flatten the capabilities jsonb into the envelope `data`.

    Numeric ceilings (`vcpus`, `memory_mb`, `concurrent_allocation_cap`) are emitted as
    native JSON ints (ADR-0263); `arch` stays a string and `transports` a comma-joined
    string. `capability_view.scalar` is `Any` over jsonb, so a numeric key is coerced with
    `int`; a missing or non-coercible value is dropped rather than stringified.
    """
    caps = resource.capability_view
    data: dict[str, JsonValue] = {"kind": resource.kind.value}
    arch = caps.scalar("arch")
    if arch is not None:
        data["arch"] = str(arch)
    for key in _INT_CAP_KEYS:
        value = caps.scalar(key)
        try:
            data[key] = int(value)
        except TypeError, ValueError:
            continue
    transports = caps.scalar("transports")
    if isinstance(transports, (list, tuple)):
        data["transports"] = ",".join(str(t) for t in transports)
    # The advertised guest CPU baseline (ADR-0368) — agent-facing, unlike guest_arches. Present
    # only on remote hosts that advertise a host-model CPU; omitted (never null) otherwise.
    host_cpu = caps.host_cpu()
    if host_cpu is not None:
        cpu_data: dict[str, JsonValue] = {key: str(value) for key, value in host_cpu.items()}
        data["host_cpu"] = cpu_data
    return data


def resource_envelope(resource: Resource, *, next_actions: list[str]) -> ToolResponse:
    return ToolResponse.success(
        str(resource.id),
        resource.status.value,
        suggested_next_actions=next_actions,
        data=resource_capability_data(resource),
    )
