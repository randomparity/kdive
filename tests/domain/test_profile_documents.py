"""Pin the domain-owned JSON document aliases for persisted profile columns."""

from __future__ import annotations

from collections.abc import Mapping

from kdive.domain import profile_documents


def test_json_object_alias_resolves_to_mapping() -> None:
    assert profile_documents.JsonObject.__value__ == Mapping[str, object]


def test_serialized_profile_aliases_exist_and_chain_to_json_object() -> None:
    for name in (
        "SerializedProvisioningProfile",
        "SerializedBuildProfile",
        "SerializedExpectedBootFailure",
    ):
        alias = getattr(profile_documents, name)
        assert alias.__value__ is profile_documents.JsonObject
