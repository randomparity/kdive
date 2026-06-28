from __future__ import annotations

import pytest

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.provider_schema import assert_kind_composed, project_tool_schema
from kdive.mcp.tool_payloads import AllocationRequestPayload
from kdive.profiles.provisioning import ProvisioningProfile

LOCAL = ResourceKind.LOCAL_LIBVIRT
REMOTE = ResourceKind.REMOTE_LIBVIRT
FAULT = ResourceKind.FAULT_INJECT


# The two surfaces narrow via DIFFERENT $defs (verified against the real schemas):
#   - allocations.request: `$defs.ResourceKind` is a named enum (the kind selector).
#   - systems.define/provision: `$defs.ProviderSection.properties` keyed by alias; the profile
#     schema has NO `ResourceKind` $def. So enum tests source from the allocation payload and
#     section tests source from the profile.
def _allocation_schema() -> dict:
    return AllocationRequestPayload.model_json_schema()


def _profile_schema() -> dict:
    return ProvisioningProfile.model_json_schema()


def test_resource_kind_enum_narrows_to_live_set() -> None:
    schema = _allocation_schema()
    assert set(schema["$defs"]["ResourceKind"]["enum"]) == {
        "local-libvirt",
        "fault-inject",
        "remote-libvirt",
    }
    projected = project_tool_schema(schema, frozenset({LOCAL}))
    assert projected["$defs"]["ResourceKind"]["enum"] == ["local-libvirt"]


def test_profile_schema_has_no_resource_kind_def() -> None:
    # Pins the asymmetry: the section union is alias-keyed, not a ResourceKind enum.
    assert "ResourceKind" not in _profile_schema()["$defs"]


def test_provider_section_properties_narrow_to_live_aliases() -> None:
    schema = _profile_schema()
    props = schema["$defs"]["ProviderSection"]["properties"]
    assert {"local-libvirt", "remote-libvirt", "fault-inject"} <= set(props)
    projected = project_tool_schema(schema, frozenset({LOCAL, REMOTE}))
    kept = set(projected["$defs"]["ProviderSection"]["properties"])
    assert "fault-inject" not in kept
    assert {"local-libvirt", "remote-libvirt"} <= kept


def test_projection_does_not_mutate_the_input() -> None:
    schema = _allocation_schema()
    before = schema["$defs"]["ResourceKind"]["enum"][:]
    project_tool_schema(schema, frozenset({LOCAL}))
    assert schema["$defs"]["ResourceKind"]["enum"] == before


def test_empty_set_narrows_each_surface() -> None:
    alloc = project_tool_schema(_allocation_schema(), frozenset())
    assert alloc["$defs"]["ResourceKind"]["enum"] == []
    profile = project_tool_schema(_profile_schema(), frozenset())
    assert profile["$defs"]["ProviderSection"]["properties"] == {}


def test_schema_without_defs_is_returned_unchanged() -> None:
    assert project_tool_schema({"type": "object"}, frozenset({LOCAL})) == {"type": "object"}


def test_assert_kind_composed_accepts_composed() -> None:
    assert_kind_composed(LOCAL, frozenset({LOCAL, REMOTE}))  # no raise


def test_assert_kind_composed_rejects_non_composed() -> None:
    with pytest.raises(CategorizedError) as exc:
        assert_kind_composed(FAULT, frozenset({LOCAL}))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["kind"] == "fault-inject"
    assert exc.value.details["available"] == ["local-libvirt"]


def test_assert_kind_composed_empty_set_message() -> None:
    with pytest.raises(CategorizedError) as exc:
        assert_kind_composed(LOCAL, frozenset())
    assert "no providers configured" in str(exc.value)
