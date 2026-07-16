"""Tests for the x86-64 CPU model -> baseline-level table and disable-guard (ADR-0368)."""

from __future__ import annotations

from kdive.domain.platform.cpu_baseline import baseline_level


def test_known_model_maps_to_level():
    assert baseline_level("Skylake-Client-IBRS", []) == "x86-64-v3"
    assert baseline_level("Nehalem", []) == "x86-64-v2"
    assert baseline_level("Cascadelake-Server", []) == "x86-64-v4"


def test_unknown_model_is_none():
    assert baseline_level("SomeFutureModel-v9", []) is None


def test_disable_guard_omits_level_when_defining_feature_stripped():
    # A v3 model with avx2 disabled by host-model must not advertise v3.
    assert baseline_level("Skylake-Client-IBRS", ["avx2"]) is None


def test_disable_of_unrelated_feature_keeps_level():
    assert baseline_level("Skylake-Client-IBRS", ["md-clear"]) == "x86-64-v3"


def test_v4_disable_guard_on_avx512f():
    assert baseline_level("Cascadelake-Server", ["avx512f"]) is None
