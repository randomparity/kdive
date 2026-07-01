"""_mac_tag mapping and the registry-iterated anti-drift guard (ADR-0287)."""

from __future__ import annotations

import pytest

from kdive.domain.catalog.images import Capability
from kdive.images.families.base import _mac_tag


def test_mac_tag_selinux_permissive() -> None:
    assert _mac_tag("selinux-permissive") == Capability.SELINUX


def test_mac_tag_apparmor() -> None:
    assert _mac_tag("apparmor") == Capability.APPARMOR


def test_mac_tag_unmapped_raises_naming_posture() -> None:
    with pytest.raises(ValueError, match="tomoyo"):
        _mac_tag("tomoyo")
