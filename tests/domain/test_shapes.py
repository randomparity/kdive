"""Tests for pure shape sizing value types (ADR-0067)."""

from __future__ import annotations

from kdive.domain.lifecycle.shapes import ResolvedSizing, ShapeSizing


def test_shape_sizing_does_not_carry_cost_class() -> None:
    # A shape fixes size only; cost_class stays host-resolved (ADR-0067), so the resolved
    # tuple exposes no cost_class field a caller could mistake for one.
    assert "cost_class" not in ShapeSizing.model_fields
    assert set(ShapeSizing.model_fields) == {"vcpus", "memory_mb", "disk_gb", "pcie_match"}


def test_resolved_sizing_carries_priced_size_and_shape_label() -> None:
    sizing = ResolvedSizing(vcpus=4, memory_gb=8, disk_gb=40, shape="large")

    assert sizing.vcpus == 4
    assert sizing.memory_gb == 8
    assert sizing.disk_gb == 40
    assert sizing.pcie_match is None
    assert sizing.shape == "large"
