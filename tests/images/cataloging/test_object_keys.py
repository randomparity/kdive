"""Direct unit tests for the shared image object-key layout (ADR-0317/0336)."""

from __future__ import annotations

from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.catalog.images import ImageVisibility
from kdive.images.cataloging.object_keys import (
    IMAGE_RETENTION_CLASS,
    config_object_key,
    config_write_request,
    object_write_request,
    owner_kind_segment,
)


def test_public_image_keys_provider_directly() -> None:
    assert owner_kind_segment("libvirt", ImageVisibility.PUBLIC, "proj") == "libvirt"


def test_private_image_folds_owner_into_segment() -> None:
    assert owner_kind_segment("libvirt", ImageVisibility.PRIVATE, "proj") == "libvirt__proj"


def test_private_image_without_owner_keys_provider_directly() -> None:
    assert owner_kind_segment("libvirt", ImageVisibility.PRIVATE, None) == "libvirt"


def test_config_key_is_public_layout_without_owner_segment() -> None:
    key = config_object_key("libvirt", "fedora", "x86_64", ImageVisibility.PUBLIC, None)
    assert key == "images/libvirt/fedora/x86_64.config"


def test_private_config_key_carries_owner_segment() -> None:
    key = config_object_key("libvirt", "fedora", "x86_64", ImageVisibility.PRIVATE, "proj")
    assert key == "images/libvirt__proj/fedora/x86_64.config"


def test_config_write_request_key_matches_config_object_key() -> None:
    args = ("libvirt", "fedora", "x86_64", ImageVisibility.PUBLIC, None)
    request = config_write_request(*args, config=b"CONFIG_X=y")
    assert request.key() == config_object_key(*args)
    assert request.data == b"CONFIG_X=y"


def test_object_write_request_stamps_shared_retention_and_sensitivity() -> None:
    request = object_write_request(
        "libvirt", "fedora", "x86_64", ImageVisibility.PUBLIC, None, data=b"d", suffix="qcow2"
    )
    assert request.key() == "images/libvirt/fedora/x86_64.qcow2"
    assert request.tenant == "images"
    assert request.retention_class == IMAGE_RETENTION_CLASS == "image"
    assert request.sensitivity is Sensitivity.REDACTED


def test_qcow2_and_config_siblings_share_prefix_and_differ_only_in_suffix() -> None:
    args = ("libvirt", "fedora", "x86_64", ImageVisibility.PRIVATE, "proj")
    qcow2 = object_write_request(*args, data=b"", suffix="qcow2").key()
    config = object_write_request(*args, data=b"", suffix="config").key()
    assert qcow2.rsplit(".", 1)[0] == config.rsplit(".", 1)[0]
    assert qcow2.endswith(".qcow2")
    assert config.endswith(".config")
