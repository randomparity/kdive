"""Tests for the per-job provider-kind contextvar (ADR-0191 F)."""

from __future__ import annotations

from kdive.jobs.provider_context import clear_provider_kind, set_provider_kind, take_provider_kind


def test_set_then_take_returns_value_and_clears() -> None:
    set_provider_kind("local-libvirt")
    assert take_provider_kind() == "local-libvirt"
    assert take_provider_kind() is None  # take clears


def test_clear_resets() -> None:
    set_provider_kind("remote-libvirt")
    clear_provider_kind()
    assert take_provider_kind() is None
