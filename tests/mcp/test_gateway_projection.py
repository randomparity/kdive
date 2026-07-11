"""TDD tests for describe_tool schema narrowing in tools.search (ADR-0269, Task 5)."""

from __future__ import annotations

from typing import Any, cast

from kdive.domain.catalog.resources import ResourceKind
from kdive.mcp.schema.tool_payloads import AllocationRequestPayload
from kdive.mcp.tools.gateway import describe_tool
from kdive.profiles.provisioning import ProvisioningProfile


class _FakeTool:
    # project_listed_tool() calls tool.model_copy(update=...) for NARROWED_TOOLS, so the stub
    # must support it (mirrors the _FakeTool in tests/mcp/middleware/test_exposure_projection.py).
    def __init__(self, name: str, parameters: dict) -> None:
        self.name = name
        self.description = name
        self.parameters = parameters

    def model_copy(self, *, update: dict) -> _FakeTool:
        return _FakeTool(self.name, update["parameters"])


def test_describe_narrows_allocation_kind_enum() -> None:
    tool = _FakeTool("allocations.request", AllocationRequestPayload.model_json_schema())
    described = describe_tool(tool, frozenset({ResourceKind.LOCAL_LIBVIRT}))  # ty: ignore[invalid-argument-type]
    schema = cast("dict[str, Any]", described["input_schema"])
    assert schema["$defs"]["ResourceKind"]["enum"] == ["local-libvirt"]


def test_describe_narrows_systems_section_props() -> None:
    tool = _FakeTool("systems.define", ProvisioningProfile.model_json_schema())
    described = describe_tool(tool, frozenset({ResourceKind.LOCAL_LIBVIRT}))  # ty: ignore[invalid-argument-type]
    schema = cast("dict[str, Any]", described["input_schema"])
    props = set(schema["$defs"]["ProviderSection"]["properties"])
    assert props == {"local-libvirt"}
