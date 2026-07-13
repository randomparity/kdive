"""Accelerator-keyed boot-deadline multiplier (ADR-0341, #1143)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import kdive.config as config
from kdive.providers.local_libvirt.lifecycle.deadlines import tcg_deadline_multiplier
from kdive.providers.local_libvirt.settings import LIBVIRT_TCG_DEADLINE_MULTIPLIER


@pytest.fixture(autouse=True)
def _isolate_config() -> Iterator[None]:
    yield
    config.reset()


def test_kvm_is_unscaled() -> None:
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "10.0"})
    assert tcg_deadline_multiplier("kvm") == 1.0


def test_tcg_uses_configured_multiplier() -> None:
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "7.0"})
    assert tcg_deadline_multiplier("tcg") == 7.0


def test_none_accel_falls_back_to_multiplier() -> None:
    # TCG-safe fallback: an unrecorded accel gets the generous (scaled) deadline.
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "7.0"})
    assert tcg_deadline_multiplier(None) == 7.0


def test_unknown_accel_falls_back_to_multiplier() -> None:
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "7.0"})
    assert tcg_deadline_multiplier("kvm-hv") == 7.0


def test_default_multiplier_is_ten() -> None:
    config.load({})  # no override → setting default
    assert tcg_deadline_multiplier("tcg") == 10.0


def test_kvm_never_reads_config() -> None:
    # A malformed multiplier must not fail a KVM boot: the KVM path short-circuits before
    # touching configuration, so an over-optimistic operator value can never break the fast path.
    config.load({LIBVIRT_TCG_DEADLINE_MULTIPLIER.name: "not-a-float"})
    assert tcg_deadline_multiplier("kvm") == 1.0
