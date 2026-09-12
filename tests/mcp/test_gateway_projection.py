"""TDD tests for describe_tool schema narrowing in tools.search (ADR-0269, Task 5)."""

from __future__ import annotations

from typing import Any, cast

from kdive.domain.catalog.resources import ResourceKind
from kdive.mcp.schema.tool_payloads import AllocationRequestPayload
from kdive.mcp.tools.gateway import SearchDetail, describe_tool
from kdive.profiles.provisioning import ProvisioningProfile
from tests.mcp.conftest import FakeTool


def test_describe_narrows_allocation_kind_enum() -> None:
    tool = FakeTool(
        "allocations.request",
        AllocationRequestPayload.model_json_schema(),
        description="allocations.request",
    )
    described = describe_tool(
        tool,  # ty: ignore[invalid-argument-type]
        frozenset({ResourceKind.LOCAL_LIBVIRT}),
        detail=SearchDetail.FULL,
    )
    schema = cast("dict[str, Any]", described["input_schema"])
    assert schema["$defs"]["ResourceKind"]["enum"] == ["local-libvirt"]


def test_parameters_tier_reads_the_projected_schema(monkeypatch: Any) -> None:
    """The ``parameters`` digest is taken from the projected schema, not the raw one.

    The live projection narrows only nested ``$defs`` (ADR-0632 §1), so comparing real
    projected against real unprojected output is byte-identical and could not fail. The stub
    below drops a top-level property instead, which fails against any implementation that
    digests ``tool.parameters`` directly.
    """
    import kdive.mcp.tools.gateway as gateway_module

    kept: dict[str, Any] = {"type": "string"}
    raw: dict[str, Any] = {
        "properties": {"kept": kept, "dropped": {"type": "string"}},
        "required": ["kept"],
    }

    def _drop_one(tool: Any, kinds: Any) -> Any:
        narrowed: dict[str, Any] = {"properties": {"kept": kept}, "required": ["kept"]}
        return tool.model_copy(update={"parameters": narrowed})

    monkeypatch.setattr(gateway_module, "project_listed_tool", _drop_one)

    described = describe_tool(
        FakeTool("allocations.request", raw, description="allocations.request"),  # ty: ignore[invalid-argument-type]
        frozenset({ResourceKind.LOCAL_LIBVIRT}),
        detail=SearchDetail.PARAMETERS,
    )

    names = [e["name"] for e in cast("list[dict[str, Any]]", described["parameters"])]
    assert names == ["kept"], f"the projected-away property must not be advertised: {names}"


def test_describe_narrows_systems_section_props() -> None:
    tool = FakeTool(
        "systems.provision",
        ProvisioningProfile.model_json_schema(),
        description="systems.provision",
    )
    described = describe_tool(
        tool,  # ty: ignore[invalid-argument-type]
        frozenset({ResourceKind.LOCAL_LIBVIRT}),
        detail=SearchDetail.FULL,
    )
    schema = cast("dict[str, Any]", described["input_schema"])
    props = set(schema["$defs"]["ProviderSection"]["properties"])
    assert props == {"local-libvirt"}
