from __future__ import annotations

import pytest

from kdive.components.references import (
    ArtifactComponentRef,
    CatalogComponentRef,
    ComponentUploadRef,
    LocalComponentRef,
    parse_component_ref,
)
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_parse_local_ref_requires_absolute_path() -> None:
    ref = parse_component_ref(
        {
            "kind": "local",
            "path": "/var/lib/kdive/rootfs/base.qcow2",
            "sha256": "sha256:" + "0" * 64,
        }
    )

    assert isinstance(ref, LocalComponentRef)
    assert ref.path == "/var/lib/kdive/rootfs/base.qcow2"


def test_parse_artifact_ref() -> None:
    ref = parse_component_ref(
        {"kind": "artifact", "artifact_id": "00000000-0000-0000-0000-000000000000"}
    )

    assert isinstance(ref, ArtifactComponentRef)


def test_parse_component_upload_ref() -> None:
    ref = parse_component_ref(
        {"kind": "component-upload", "upload_id": "00000000-0000-0000-0000-000000000000"}
    )

    assert isinstance(ref, ComponentUploadRef)


def test_parse_catalog_ref() -> None:
    ref = parse_component_ref({"kind": "catalog", "provider": "local-libvirt", "name": "fedora"})

    assert isinstance(ref, CatalogComponentRef)
    assert ref.provider == "local-libvirt"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "local", "path": "relative.img"},
        {"kind": "local", "path": "/x", "sha256": "deadbeef"},
        {"kind": "artifact", "artifact_id": "not-a-uuid"},
        {"kind": "component-upload", "upload_id": "not-a-uuid"},
        {"kind": "catalog", "provider": "remote-libvirt", "name": ""},
        {"kind": "url", "url": "https://example.invalid/x.qcow2"},
    ],
)
def test_parse_component_ref_maps_invalid_payloads_to_config_error(
    payload: dict[str, object],
) -> None:
    with pytest.raises(CategorizedError) as caught:
        parse_component_ref(payload)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_parse_component_ref_error_carries_message_and_pydantic_errors() -> None:
    # The wrapped error keeps a stable human message and surfaces the pydantic validation errors
    # under a "errors" details key for the caller to relay.
    with pytest.raises(CategorizedError) as caught:
        parse_component_ref({"kind": "local", "path": "/x", "sha256": "deadbeef"})

    assert str(caught.value) == "invalid component reference"
    errors = caught.value.details["errors"]
    assert isinstance(errors, list) and errors


def test_parse_component_ref_errors_omit_url_and_input_noise() -> None:
    # The relayed error dicts must not leak the documentation URL or the (potentially sensitive)
    # raw input value; include_url/include_input are pinned off.
    with pytest.raises(CategorizedError) as caught:
        parse_component_ref({"kind": "local", "path": "/x", "sha256": "deadbeef"})

    for error in caught.value.details["errors"]:
        assert "url" not in error
        assert "input" not in error


def test_parse_component_ref_surfaces_sha256_validator_message() -> None:
    # A malformed sha256 must produce the validator's specific guidance, not a generic message.
    with pytest.raises(CategorizedError) as caught:
        parse_component_ref({"kind": "local", "path": "/x", "sha256": "deadbeef"})

    messages = [error["msg"] for error in caught.value.details["errors"]]
    assert "Value error, sha256 must be 'sha256:<64 lowercase hex chars>'" in messages
