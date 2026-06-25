"""Shape XOR full-custom-triple validation on the allocation request payload (#161).

A request names a shape NAME or a full custom ``{vcpus, memory_gb, disk_gb}`` triple, never
both and never neither. A partial custom triple (e.g. missing ``disk_gb``) is rejected at
this boundary as a structural error, so it can never reach a NULL ``requested_disk_gb``
snapshot that would silently disable the size unification (ADR-0067).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from kdive.domain.catalog.resources import ResourceKind
from kdive.mcp.tool_payloads import (
    AllocationRequestPayload,
    EstimateRequestPayload,
    ResourceByKind,
    ResourceByPool,
)


def test_published_schema_shape_description_names_shapes_list_and_xor_rule() -> None:
    # #473 acceptance: the published input schema documents `shape` — a pointer to shapes.list
    # and the XOR rule JSON Schema cannot express.
    schema = AllocationRequestPayload.model_json_schema()
    description = schema["properties"]["shape"]["description"]
    assert "shapes.list" in description
    assert "mutually exclusive" in description


def test_shape_only_is_valid() -> None:
    payload = AllocationRequestPayload.model_validate({"shape": "medium"})
    assert payload.shape == "medium"
    assert payload.vcpus is None
    assert payload.memory_gb is None
    assert payload.disk_gb is None


def test_full_custom_triple_is_valid() -> None:
    payload = AllocationRequestPayload.model_validate({"vcpus": 2, "memory_gb": 4, "disk_gb": 20})
    assert payload.shape is None
    assert (payload.vcpus, payload.memory_gb, payload.disk_gb) == (2, 4, 20)


def test_by_kind_selector_uses_resource_kind_enum() -> None:
    payload = AllocationRequestPayload.model_validate({"shape": "medium"})
    assert isinstance(payload.resource, ResourceByKind)
    assert payload.resource.kind is ResourceKind.LOCAL_LIBVIRT

    explicit = AllocationRequestPayload.model_validate(
        {
            "shape": "medium",
            "resource": {"mode": "kind", "kind": "fault-inject"},
        }
    )
    assert isinstance(explicit.resource, ResourceByKind)
    assert explicit.resource.kind is ResourceKind.FAULT_INJECT


def test_by_pool_selector_parses() -> None:
    payload = AllocationRequestPayload.model_validate(
        {"shape": "medium", "resource": {"mode": "pool", "pool": "big-remote"}}
    )
    assert isinstance(payload.resource, ResourceByPool)
    assert payload.resource.pool == "big-remote"


def test_by_pool_selector_rejects_empty_pool() -> None:
    with pytest.raises(ValidationError):
        AllocationRequestPayload.model_validate(
            {"shape": "medium", "resource": {"mode": "pool", "pool": ""}}
        )


def _xor_error_entry(payload: dict[str, object]) -> tuple[str, object]:
    """Validate ``payload`` and return the sole entry's ``(type, ctx['both'])``."""
    try:
        AllocationRequestPayload.model_validate(payload)
    except ValidationError as exc:
        entries = exc.errors()
        assert len(entries) == 1
        entry = entries[0]
        ctx = entry.get("ctx") or {}
        return str(entry["type"]), ctx.get("both")
    raise AssertionError("expected a ValidationError")


def test_shape_and_custom_together_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AllocationRequestPayload.model_validate(
            {"shape": "medium", "vcpus": 2, "memory_gb": 4, "disk_gb": 20}
        )


def test_neither_shape_nor_custom_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AllocationRequestPayload.model_validate({})


def test_xor_violation_carries_stable_error_type_and_both_flag() -> None:
    # The shape-XOR violation raises a typed error (not a bare value_error) so the binding
    # middleware can distinguish it from a field-level error (#473, ADR-0132).
    assert _xor_error_entry({"shape": "medium", "vcpus": 2, "memory_gb": 4, "disk_gb": 20}) == (
        "shape_xor_custom",
        True,
    )
    assert _xor_error_entry({}) == ("shape_xor_custom", False)
    assert _xor_error_entry({"vcpus": 2}) == ("shape_xor_custom", False)


@pytest.mark.parametrize(
    "partial",
    [
        {"vcpus": 2, "memory_gb": 4},
        {"vcpus": 2, "disk_gb": 20},
        {"memory_gb": 4, "disk_gb": 20},
        {"vcpus": 2},
        {"disk_gb": 20},
    ],
)
def test_partial_custom_triple_is_rejected(partial: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        AllocationRequestPayload.model_validate(partial)


def test_shape_with_one_custom_field_is_rejected() -> None:
    # A shape plus any custom sizing field is "both sides set" — rejected.
    with pytest.raises(ValidationError):
        AllocationRequestPayload.model_validate({"shape": "small", "vcpus": 2})


def test_estimate_payload_keeps_custom_only_sizing() -> None:
    # Estimate is a read-side custom-only price; it never gained shapes (ADR-0067), so its
    # vcpus/memory_gb stay required and it has no shape/disk_gb fields.
    payload = EstimateRequestPayload.model_validate({"vcpus": 1, "memory_gb": 2, "window": 1})
    assert (payload.vcpus, payload.memory_gb) == (1, 2)
    assert "shape" not in EstimateRequestPayload.model_fields
    with pytest.raises(ValidationError):
        EstimateRequestPayload.model_validate({"window": 1})


def _assert_window_renders_typed_positive_hours(model: type[BaseModel]) -> None:
    window = model.model_json_schema()["properties"]["window"]
    branch_types = {branch.get("type") for branch in window["anyOf"]}
    assert "number" in branch_types
    assert window["examples"] == [24]
    assert "hours" in window["description"].lower()


def test_estimate_window_schema_is_typed_positive_hours() -> None:
    # #807: `window` must render with a numeric type and an example so a black-box caller
    # sees a positive number of lease hours, not an opaque `{}` (and not an ISO pair).
    _assert_window_renders_typed_positive_hours(EstimateRequestPayload)


def test_allocation_window_schema_is_typed_positive_hours() -> None:
    # The shared SelectorPayload window is tightened the same way so admission also exposes
    # a typed, documented window (optional on the allocation request).
    _assert_window_renders_typed_positive_hours(AllocationRequestPayload)


@pytest.mark.parametrize("bad", [0, -3, "not-a-number", "NaN", "Infinity"])
def test_estimate_payload_rejects_non_positive_or_nonfinite_window(bad: object) -> None:
    # The typed `gt=0` finite window rejects zero, negative, unparseable, and non-finite
    # values at the wire boundary (the binding middleware maps this to the caller).
    with pytest.raises(ValidationError):
        EstimateRequestPayload.model_validate({"vcpus": 1, "memory_gb": 1, "window": bad})


@pytest.mark.parametrize("bad", [0, -3, "not-a-number", "NaN", "Infinity"])
def test_allocation_payload_rejects_non_positive_or_nonfinite_window(bad: object) -> None:
    # A supplied allocation window is rejected at the same boundary (the field stays
    # optional, so an omitted window still takes the configured default at admission).
    with pytest.raises(ValidationError):
        AllocationRequestPayload.model_validate(
            {"vcpus": 1, "memory_gb": 1, "disk_gb": 10, "window": bad}
        )


def test_estimate_payload_accepts_fractional_and_string_window() -> None:
    # A numeric string and a fractional value are accepted and coerced to Decimal.
    assert EstimateRequestPayload.model_validate(
        {"vcpus": 1, "memory_gb": 1, "window": "24"}
    ).window == Decimal("24")
    assert EstimateRequestPayload.model_validate(
        {"vcpus": 1, "memory_gb": 1, "window": 1.5}
    ).window == Decimal("1.5")
