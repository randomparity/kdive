"""Tests for the build-profile schema (`kdive.profiles.build`)."""

from __future__ import annotations

import copy
from typing import Any, cast

import pytest
from pydantic import ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import BuildProfile, dump_build_profile

_VALID: dict[str, Any] = {"schema_version": 1}


def _valid() -> dict[str, Any]:
    """A fresh deep copy of the canonical valid profile, safe to mutate."""
    return copy.deepcopy(_VALID)


def _expect_configuration_error(data: Any) -> None:
    """Assert that parsing ``data`` fails as a CONFIGURATION_ERROR."""
    with pytest.raises(CategorizedError) as caught:
        BuildProfile.parse(data)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_valid_profile_parses() -> None:
    profile = BuildProfile.parse(_valid())
    assert isinstance(profile, BuildProfile)
    assert profile.schema_version == 1


def test_missing_schema_version_raises_configuration_error() -> None:
    _expect_configuration_error({})


def test_unknown_field_rejected() -> None:
    # The flat profile forbids extra fields: a stray source-tree field (e.g. the retired
    # kernel_source_ref) is a configuration error at the lane boundary.
    data = _valid()
    data["kernel_source_ref"] = "linux-6.9"
    _expect_configuration_error(data)


@pytest.mark.parametrize("payload", [None, [], "not-a-mapping", 42])
def test_non_mapping_input_rejected(payload: Any) -> None:
    _expect_configuration_error(payload)


def test_unreadable_schema_version_rejected() -> None:
    data = _valid()
    data["schema_version"] = 2
    _expect_configuration_error(data)


@pytest.mark.parametrize("value", [True, "1", 1.0])
def test_non_int_schema_version_rejected(value: object) -> None:
    # A bool/str/float must not coerce to version 1 (the Literal[1] coercion trap).
    data = _valid()
    data["schema_version"] = value
    _expect_configuration_error(data)


def test_error_details_do_not_leak_submitted_values() -> None:
    data = _valid()
    data["schema_version"] = "S3CRET-LOOKING-VALUE"  # wrong type carrying a sentinel

    with pytest.raises(CategorizedError) as caught:
        BuildProfile.parse(data)

    assert "S3CRET-LOOKING-VALUE" not in str(caught.value.details)


def test_validation_error_message_is_fixed_and_details_carry_errors() -> None:
    # A structural failure maps onto the fixed wire message, and the details carry the
    # field-located Pydantic errors under the "errors" key.
    with pytest.raises(CategorizedError) as caught:
        BuildProfile.parse({})

    assert str(caught.value) == "invalid build profile"
    errors = caught.value.details["errors"]
    assert isinstance(errors, list) and errors
    entries = cast("list[dict[str, Any]]", errors)
    assert any(tuple(entry.get("loc", ())) == ("schema_version",) for entry in entries)


def test_validation_error_details_omit_input_url_and_context() -> None:
    # The wire taxonomy errors must be value-free and link-free: no submitted input, no
    # pydantic docs url, and no validator context (which can echo the input) survives.
    data = _valid()
    data["schema_version"] = "S3CRET-LOOKING-VALUE"

    with pytest.raises(CategorizedError) as caught:
        BuildProfile.parse(data)

    errors = caught.value.details["errors"]
    assert errors
    entries = cast("list[dict[str, Any]]", errors)
    for entry in entries:
        assert "input" not in entry
        assert "url" not in entry
        assert "ctx" not in entry
    assert "S3CRET-LOOKING-VALUE" not in str(caught.value.details)


def test_profile_is_frozen() -> None:
    profile = BuildProfile.parse(_valid())
    with pytest.raises(ValidationError):
        profile.schema_version = 1  # type: ignore[misc]


def test_direct_construction_bypasses_configuration_error_mapping() -> None:
    # The sanctioned door is BuildProfile.parse; constructing the model directly surfaces the
    # raw ValidationError without the CONFIGURATION_ERROR mapping.
    with pytest.raises(ValidationError):
        BuildProfile.model_validate({})


def test_dump_build_profile_round_trips() -> None:
    profile = BuildProfile.parse(_valid())
    dumped = dump_build_profile(profile)
    reparsed = BuildProfile.parse(dumped)
    assert reparsed.schema_version == profile.schema_version
