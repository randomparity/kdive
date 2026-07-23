"""Closed rootfs image-kind vocabulary shared by catalog and family customization."""

from __future__ import annotations

from typing import Literal

type RootfsImageKind = Literal["debug", "build"]

ROOTFS_IMAGE_KINDS: frozenset[RootfsImageKind] = frozenset(("debug", "build"))


def parse_rootfs_image_kind(value: str) -> RootfsImageKind | None:
    """Return ``value`` as a rootfs image kind when it is in the closed vocabulary."""
    if value in ROOTFS_IMAGE_KINDS:
        return value
    return None
