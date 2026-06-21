"""Public JSON serialization contract tests."""

from __future__ import annotations

import math
import re

import pytest

from kdive.serialization import (
    ensure_json_value,
    safe_error_details,
    validate_json_value,
)


def test_ensure_json_value_accepts_nested_json_tree() -> None:
    value = {
        "status": "ok",
        "attempts": 2,
        "ratio": 0.5,
        "enabled": True,
        "items": [{"name": "kernel", "refs": ["a", "b"]}, None],
    }

    validated = ensure_json_value(value, path="payload")

    assert validated == value


def test_ensure_json_value_validates_its_own_argument_and_reports_path() -> None:
    # ensure_json_value must validate the very value it returns (not some other object) and
    # the rejection message must name the caller's path.
    with pytest.raises(ValueError, match=re.escape("payload contains non-JSON value set")):
        ensure_json_value({1, 2}, path="payload")


@pytest.mark.parametrize("number", [math.inf, -math.inf, math.nan])
def test_validate_json_value_rejects_non_finite_numbers_with_path(number: float) -> None:
    with pytest.raises(ValueError, match=re.escape("payload.score must be finite JSON number")):
        validate_json_value({"score": number}, path="payload")


def test_validate_json_value_rejects_non_string_dict_keys_with_path() -> None:
    with pytest.raises(ValueError, match=re.escape("payload.items[0] object keys must be strings")):
        validate_json_value({"items": [{1: "rootfs"}]}, path="payload")


def test_validate_json_value_rejects_nested_invalid_values_with_path() -> None:
    invalid = {"items": [{"metadata": {"owner": object()}}]}

    with pytest.raises(
        ValueError,
        match=re.escape("payload.items[0].metadata.owner contains non-JSON value object"),
    ):
        validate_json_value(invalid, path="payload")


# ---------------------------------------------------------------------------
# safe_error_details — scalar filter + reserved `errors` widening (ADR-0123)
# ---------------------------------------------------------------------------


def test_safe_error_details_keeps_finite_scalars_and_drops_collections() -> None:
    out = safe_error_details(
        {
            "field": "rootfs",
            "flag": False,
            "count": 3,
            "ratio": 1.5,
            "nan": math.nan,
            "nested": {"x": 1},
            "list": ["a"],
        }
    )
    assert out == {"field": "rootfs", "flag": False, "count": 3, "ratio": 1.5}


def test_safe_error_details_preserves_bounded_errors_list() -> None:
    entries = [{"loc": (f"f{i}",), "msg": "bad", "type": "missing"} for i in range(25)]
    out = safe_error_details({"errors": entries})
    assert isinstance(out["errors"], list)
    assert len(out["errors"]) == 20
    assert out["errors"][0] == {"loc": ["f0"], "msg": "bad", "type": "missing"}


def test_safe_error_details_strips_input_and_ctx_from_error_entries() -> None:
    out = safe_error_details(
        {
            "errors": [
                {
                    "loc": ("provider", "kind"),
                    "msg": "field required",
                    "type": "missing",
                    "input": "SUBMITTED",
                    "ctx": {"internal": "noise"},
                    "url": "https://errors.pydantic.dev/x",
                }
            ]
        }
    )
    assert out["errors"] == [
        {"loc": ["provider", "kind"], "msg": "field required", "type": "missing"}
    ]


def test_safe_error_details_errors_loc_keeps_int_segments() -> None:
    out = safe_error_details({"errors": [{"loc": ("items", 2, "name"), "msg": "m", "type": "t"}]})
    assert out["errors"] == [{"loc": ["items", 2, "name"], "msg": "m", "type": "t"}]


def test_safe_error_details_drops_non_mapping_error_entries() -> None:
    out = safe_error_details({"errors": ["not-a-dict", {"loc": ("a",), "msg": "m", "type": "t"}]})
    assert out["errors"] == [{"loc": ["a"], "msg": "m", "type": "t"}]


def test_safe_error_details_keeps_scalar_keys_that_follow_the_errors_key() -> None:
    # The `errors` widening must not terminate the scan: scalar keys ordered after `errors`
    # in the details mapping are still filtered and kept.
    out = safe_error_details(
        {
            "errors": [{"loc": ("a",), "msg": "m", "type": "t"}],
            "field": "rootfs",
            "count": 7,
        }
    )
    assert out["field"] == "rootfs"
    assert out["count"] == 7
    assert isinstance(out["errors"], list)


def test_safe_error_details_non_list_errors_value_dropped_as_scalar() -> None:
    # An `errors` key that is not a list falls through to the scalar rule (a string survives).
    assert safe_error_details({"errors": "boom"}) == {"errors": "boom"}
    # A dict under `errors` (not a list) is dropped like any non-scalar.
    assert safe_error_details({"errors": {"x": 1}}) == {}
