"""Shared image format contract for catalog rootfs images."""

from __future__ import annotations

from typing import Literal

type ImageFormat = Literal["qcow2"]

SUPPORTED_IMAGE_FORMATS: tuple[ImageFormat, ...] = ("qcow2",)
