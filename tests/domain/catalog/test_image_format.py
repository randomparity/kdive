"""Pin the shared catalog rootfs image-format contract."""

from __future__ import annotations

from kdive.domain.catalog import image_format


def test_supported_image_formats_is_qcow2_only() -> None:
    assert image_format.SUPPORTED_IMAGE_FORMATS == ("qcow2",)


def test_qcow2_is_a_supported_format() -> None:
    assert "qcow2" in image_format.SUPPORTED_IMAGE_FORMATS
