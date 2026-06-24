"""Pin the shared JSON document aliases for profile-shaped API input boundaries."""

from __future__ import annotations

from collections.abc import Mapping

from kdive.profiles import types


def test_json_object_input_alias_resolves_to_mapping() -> None:
    assert types.JsonObjectInput.__value__ == Mapping[str, object]


def test_input_aliases_exist_and_chain_to_json_object_input() -> None:
    for name in (
        "ProvisioningProfileInput",
        "BuildProfileInput",
        "ExpectedBootFailureInput",
    ):
        alias = getattr(types, name)
        assert alias.__value__ is types.JsonObjectInput
