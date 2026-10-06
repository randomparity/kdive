"""Tests for the pure live-drgn-introspection capability predicate (ADR-0328)."""

from __future__ import annotations

import pytest

from kdive.images.drgn_support import (
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


def test_any_parsed_version_is_capable_and_names_the_vmlinux_upload() -> None:
    for version in ("0.0.22", "0.0.31", "0.2.0"):
        cap = live_drgn_capability(drgn_version=version, drgn_tooling=True)
        assert cap.status == "capable", version
        assert cap.drgn_version == version
        assert cap.min_drgn_required is None
        assert "vmlinux" in cap.note


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
