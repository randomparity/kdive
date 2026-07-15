"""Unit coverage for the require_guest_arch skip gate (#1154, ADR-0353)."""

from __future__ import annotations

import pytest

from tests.integration.live_stack.conftest import require_guest_arch


def test_returns_none_when_emulator_on_path() -> None:
    # A known arch whose emulator `which` resolves → gate passes (returns None, no skip).
    assert require_guest_arch("ppc64le", which=lambda _binary: "/usr/bin/qemu-system-ppc64") is None


def test_skips_when_emulator_absent() -> None:
    # Known arch, emulator not on PATH → clean skip.
    with pytest.raises(pytest.skip.Exception):
        require_guest_arch("ppc64le", which=lambda _binary: None)


def test_skips_when_arch_unknown_to_map() -> None:
    # An arch with no qemu_system_binary entry → clean skip (defensive floor, never a crash).
    with pytest.raises(pytest.skip.Exception):
        require_guest_arch("s390x", which=lambda _binary: "/usr/bin/whatever")
