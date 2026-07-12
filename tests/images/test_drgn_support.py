"""Tests for the pure live-drgn-introspection capability predicate (ADR-0328)."""

from __future__ import annotations

import pytest

from kdive.images.drgn_support import (
    BTF_CAPABLE_DRGN,
    DrgnVersion,
    live_drgn_capability,
)


def test_parse_dotted_triple() -> None:
    assert DrgnVersion.parse("0.0.31") == DrgnVersion(0, 0, 31)


def test_parse_from_banner() -> None:
    assert DrgnVersion.parse("drgn 0.0.33 (using Python 3.14)") == DrgnVersion(0, 0, 33)


def test_parse_from_package_stamp() -> None:
    assert DrgnVersion.parse("python-drgn-0.0.31-4.el10") == DrgnVersion(0, 0, 31)


def test_parse_bare_pair_defaults_patch() -> None:
    assert DrgnVersion.parse("0.1") == DrgnVersion(0, 1, 0)


def test_parse_rejects_non_version() -> None:
    with pytest.raises(ValueError, match="unrecognized drgn version"):
        DrgnVersion.parse("not a version")


def test_ordering_is_total() -> None:
    assert DrgnVersion(0, 0, 22) < DrgnVersion(0, 0, 31) < DrgnVersion(0, 1, 0)


def test_threshold_is_0_0_31() -> None:
    assert DrgnVersion(0, 0, 31) == BTF_CAPABLE_DRGN


def test_capable_at_and_above_threshold() -> None:
    for version in ("0.0.31", "0.0.33", "0.1.0"):
        cap = live_drgn_capability(drgn_version=version, drgn_tooling=True)
        assert cap.status == "capable", version
        assert cap.min_drgn_required == "0.0.31"
        assert cap.note == ""


def test_incapable_below_threshold() -> None:
    cap = live_drgn_capability(drgn_version="0.0.22", drgn_tooling=True)
    assert cap.status == "incapable"
    assert cap.drgn_version == "0.0.22"
    assert cap.min_drgn_required == "0.0.31"
    assert "0.0.22" in cap.note and "0.0.31" in cap.note


def test_not_applicable_without_tooling() -> None:
    cap = live_drgn_capability(drgn_version="0.0.33", drgn_tooling=False)
    assert cap.status == "not_applicable"
    assert cap.min_drgn_required is None


def test_unverified_when_version_absent() -> None:
    cap = live_drgn_capability(drgn_version=None, drgn_tooling=True)
    assert cap.status == "unverified"
    assert cap.min_drgn_required is None
    assert cap.note


def test_unverified_when_version_unparseable() -> None:
    cap = live_drgn_capability(drgn_version="unknown", drgn_tooling=True)
    assert cap.status == "unverified"
    assert "unknown" in cap.note
