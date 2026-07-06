"""Tests for the build-profile schema (`kdive.profiles.build`)."""

from __future__ import annotations

import copy
from typing import Any, cast

import pytest
from pydantic import ValidationError

from kdive.build_configs.defaults import catalog_config_ref
from kdive.components.references import CatalogComponentRef, LocalComponentRef
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import (
    BuildProfile,
    ExternalBuildProfile,
    GitKernelSource,
    ServerBuildProfile,
    dump_build_profile,
    is_git_source,
)

_VALID: dict[str, Any] = {
    "schema_version": 1,
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "config": {"kind": "local", "path": "/configs/x86_64-kdump.config"},
    "patch_ref": "file:///patches/fix.patch",
}


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
    assert isinstance(profile, ServerBuildProfile)

    assert profile.schema_version == 1
    assert isinstance(profile.kernel_source_ref, str)
    assert profile.kernel_source_ref.startswith("git+https://")
    assert isinstance(profile.config, LocalComponentRef)
    assert profile.config.kind == "local"
    assert profile.config.path == "/configs/x86_64-kdump.config"
    assert profile.patch_ref == "file:///patches/fix.patch"


def test_server_build_profile_parses_config_ref_and_profile_requirements() -> None:
    profile = BuildProfile.parse(
        {
            "schema_version": 1,
            "source": "server",
            "kernel_source_ref": "file:///home/dave/src/linux",
            "config": {"kind": "local", "path": "/var/lib/kdive/components/linux.config"},
            "profile_requirements": {
                "provider": "local-libvirt",
                "name": "console-ready_x86_64",
            },
        }
    )

    assert isinstance(profile, ServerBuildProfile)
    assert profile.config is not None
    assert profile.config.kind == "local"
    assert profile.profile_requirements is not None
    assert profile.profile_requirements.name == "console-ready_x86_64"


def test_patch_ref_defaults_to_none() -> None:
    data = _valid()
    del data["patch_ref"]

    profile = BuildProfile.parse(data)
    assert isinstance(profile, ServerBuildProfile)

    assert profile.patch_ref is None


@pytest.mark.parametrize("field", ["schema_version", "kernel_source_ref"])
def test_missing_required_field_raises_configuration_error(field: str) -> None:
    data = _valid()
    del data[field]
    _expect_configuration_error(data)


def test_omitted_config_parses_as_none() -> None:
    # config is optional (ADR-0096): an omitted ref is no longer a configuration error; it
    # defaults to the kdump catalog fragment at the build boundary, so the profile carries None.
    data = _valid()
    del data["config"]

    profile = BuildProfile.parse(data)

    assert isinstance(profile, ServerBuildProfile)
    assert profile.config is None


def test_unknown_field_rejected() -> None:
    data = _valid()
    data["unexpected"] = "x"
    _expect_configuration_error(data)


@pytest.mark.parametrize("value", ["", "   "])
@pytest.mark.parametrize("field", ["kernel_source_ref"])
def test_blank_required_string_rejected(field: str, value: str) -> None:
    data = _valid()
    data[field] = value
    _expect_configuration_error(data)


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_patch_ref_rejected(value: str) -> None:
    # patch_ref is optional, but when supplied it must be a non-empty token.
    data = _valid()
    data["patch_ref"] = value
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


def test_unknown_source_raises_with_exact_message_and_details() -> None:
    # An unrecognized build source is rejected before Pydantic with a fixed, value-free
    # error: the message and the synthetic details locate the offending field by name.
    data = _valid()
    data["source"] = "wormhole"

    with pytest.raises(CategorizedError) as caught:
        BuildProfile.parse(data)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(caught.value) == "invalid build profile"
    assert caught.value.details == {"errors": [{"loc": ["source"], "msg": "unknown build source"}]}


def test_validation_error_message_is_fixed_and_details_carry_errors() -> None:
    # A structural failure maps onto the fixed wire message, and the details carry the
    # field-located Pydantic errors under the "errors" key.
    data = _valid()
    del data["kernel_source_ref"]

    with pytest.raises(CategorizedError) as caught:
        BuildProfile.parse(data)

    assert str(caught.value) == "invalid build profile"
    errors = caught.value.details["errors"]
    assert isinstance(errors, list) and errors
    entries = cast("list[dict[str, Any]]", errors)
    assert any(tuple(entry.get("loc", ())) == ("kernel_source_ref",) for entry in entries)


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
    assert isinstance(profile, ServerBuildProfile)

    with pytest.raises(ValidationError):
        profile.kernel_source_ref = "other"


def test_direct_construction_bypasses_configuration_error_mapping() -> None:
    # The sanctioned door is BuildProfile.parse; constructing a concrete model directly
    # (here ServerBuildProfile, since BuildProfile is no longer a Pydantic model) surfaces
    # the raw ValidationError without the CONFIGURATION_ERROR mapping.
    with pytest.raises(ValidationError):
        ServerBuildProfile.model_validate({"schema_version": 1})


# ---------------------------------------------------------------------------
# Task 8: build_host + structured kernel_source_ref
# ---------------------------------------------------------------------------


def test_string_kernel_source_ref_is_not_git_source() -> None:
    profile = BuildProfile.parse(_valid())
    assert isinstance(profile, ServerBuildProfile)
    assert is_git_source(profile) is False


def test_git_kernel_source_ref_parses_and_is_detected() -> None:
    data = _valid()
    data["kernel_source_ref"] = {"git": {"remote": "https://x/y.git", "ref": "v6.1"}}
    profile = BuildProfile.parse(data)
    assert isinstance(profile, ServerBuildProfile)
    assert is_git_source(profile) is True
    assert isinstance(profile.kernel_source_ref, GitKernelSource)
    assert profile.kernel_source_ref.git.remote == "https://x/y.git"
    assert profile.kernel_source_ref.git.ref == "v6.1"


def test_git_source_missing_ref_raises_configuration_error() -> None:
    data = _valid()
    data["kernel_source_ref"] = {"git": {"remote": "https://x/y.git"}}
    with pytest.raises(CategorizedError) as caught:
        BuildProfile.parse(data)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    # Redaction guarantee: submitted values must not appear in details.
    assert "https://x/y.git" not in str(caught.value.details)


def test_git_source_extra_field_raises_configuration_error() -> None:
    data = _valid()
    data["kernel_source_ref"] = {"git": {"remote": "r", "ref": "v1"}, "extra": 1}
    _expect_configuration_error(data)


def test_git_inner_extra_field_raises_configuration_error() -> None:
    data = _valid()
    data["kernel_source_ref"] = {"git": {"remote": "r", "ref": "v1", "bonus": "x"}}
    _expect_configuration_error(data)


def test_build_host_parses() -> None:
    data = _valid()
    data["build_host"] = "fast-builder"
    profile = BuildProfile.parse(data)
    assert isinstance(profile, ServerBuildProfile)
    assert profile.build_host == "fast-builder"


def test_build_host_empty_raises_configuration_error() -> None:
    data = _valid()
    data["build_host"] = ""
    _expect_configuration_error(data)


def test_build_host_defaults_to_none() -> None:
    profile = BuildProfile.parse(_valid())
    assert isinstance(profile, ServerBuildProfile)
    assert profile.build_host is None


def test_dump_build_profile_round_trips_string_source_ref() -> None:
    profile = BuildProfile.parse(_valid())
    assert isinstance(profile, ServerBuildProfile)
    dumped = dump_build_profile(profile)
    reparsed = BuildProfile.parse(dumped)
    assert isinstance(reparsed, ServerBuildProfile)
    assert reparsed.kernel_source_ref == profile.kernel_source_ref


def test_dump_build_profile_round_trips_git_source_ref() -> None:
    data = _valid()
    data["kernel_source_ref"] = {"git": {"remote": "https://x/y.git", "ref": "v6.1"}}
    profile = BuildProfile.parse(data)
    assert isinstance(profile, ServerBuildProfile)
    dumped = dump_build_profile(profile)
    reparsed = BuildProfile.parse(dumped)
    assert isinstance(reparsed, ServerBuildProfile)
    assert isinstance(reparsed.kernel_source_ref, GitKernelSource)
    assert reparsed.kernel_source_ref.git.remote == "https://x/y.git"
    assert reparsed.kernel_source_ref.git.ref == "v6.1"


def test_existing_server_profile_without_build_host_back_compat() -> None:
    # Profiles written before build_host was introduced must still parse.
    data = {
        "schema_version": 1,
        "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    }
    profile = BuildProfile.parse(data)
    assert isinstance(profile, ServerBuildProfile)
    assert profile.build_host is None


# ---------------------------------------------------------------------------
# Task 3: bare git-URL guard on kernel_source_ref (ADR-0242)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "git:abc",
        "git://h/r",
        "git+ssh://h/r",
        "ssh://h/r",
        "https://h/r",
        "http://h/r",
        "HTTPS://h/r",
    ],
)
def test_bare_uri_kernel_source_ref_rejected(ref: str) -> None:
    data = {"schema_version": 1, "kernel_source_ref": ref}
    with pytest.raises(CategorizedError) as e:
        BuildProfile.parse(data)
    assert e.value.category is ErrorCategory.CONFIGURATION_ERROR


@pytest.mark.parametrize(
    "ref",
    [
        "linux-6.9",
        "/srv/linux",
        "git+https://git.kernel.org/linux.git#v6.9",
        "file:///src/linux",
        "git@github.com:torvalds/linux",
        "git-6.9",
    ],
)
def test_bare_label_kernel_source_ref_accepted(ref: str) -> None:
    data = {"schema_version": 1, "kernel_source_ref": ref}
    profile = BuildProfile.parse(data)
    assert isinstance(profile, ServerBuildProfile)
    assert profile.kernel_source_ref == ref


def test_structured_git_with_https_remote_not_rejected() -> None:
    data = {
        "schema_version": 1,
        "kernel_source_ref": {"git": {"remote": "https://h/r", "ref": "v6.9"}},
    }
    profile = BuildProfile.parse(data)
    assert isinstance(profile, ServerBuildProfile)
    assert isinstance(profile.kernel_source_ref, GitKernelSource)
    assert profile.kernel_source_ref.git.remote == "https://h/r"


def test_rejected_uri_error_does_not_leak_value() -> None:
    data = {"schema_version": 1, "kernel_source_ref": "https://USER-PLANTED-TOKEN@h/r"}
    with pytest.raises(CategorizedError) as e:
        BuildProfile.parse(data)
    assert "PLANTED-TOKEN" not in str(e.value.details)


# ---------------------------------------------------------------------------
# #1032: config is a source='server'-only field — the echoed config_ref pastes
# into a server build and is rejected in the external lane.
# ---------------------------------------------------------------------------


def test_echoed_config_ref_parses_in_server_lane() -> None:
    ref = catalog_config_ref("kdump").model_dump()
    profile = ServerBuildProfile.model_validate(
        {"schema_version": 1, "source": "server", "kernel_source_ref": "warm", "config": ref}
    )
    assert isinstance(profile.config, CatalogComponentRef)
    assert profile.config.name == "kdump"


def test_echoed_config_ref_rejected_in_external_lane() -> None:
    # ExternalBuildProfile has no `config` field and is extra='forbid', so pasting the
    # echoed ref into a source='external' profile is a schema error (the lane boundary).
    ref = catalog_config_ref("kdump").model_dump()
    with pytest.raises(ValidationError):
        ExternalBuildProfile.model_validate(
            {"schema_version": 1, "source": "external", "config": ref}
        )
