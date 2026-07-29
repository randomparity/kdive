"""The deployed-build fact `/readyz` reports (ADR-0482 decision 1).

`deployed_version()` must mirror `version_info()` (ADR-0041/0370) rather than open a fourth
resolution path, and must stamp the process start instant the skew preflight compares against.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kdive.health.deployed_version import deployed_version
from kdive.version import VersionInfo


def test_mirrors_version_info_rather_than_resolving_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-0482 §1: the aux listener adds no fourth version-resolution path. Whatever
    # version_info() says (baked _buildinfo, else live git, else unknown) is what /readyz says.
    #
    # Pinned against a sentinel rather than a second version_info() call: that function is
    # lru_cached, so `deployed.commit == version_info().commit` compares an object with itself
    # and would still pass if this module resolved nothing at all — on a git-less host without
    # baked build info the whole assertion degrades to `None == None`.
    sentinel = VersionInfo(version="9.9.9", commit="cafebabe", is_release=True)
    monkeypatch.setattr("kdive.health.deployed_version.version_info", lambda: sentinel)
    deployed = deployed_version()
    assert deployed.version == "9.9.9"
    assert deployed.commit == "cafebabe"
    assert deployed.is_release is True


def test_reports_an_unresolvable_commit_as_none_rather_than_inventing_one() -> None:
    unknown = VersionInfo(version="0.0.0", commit=None, is_release=False)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("kdive.health.deployed_version.version_info", lambda: unknown)
        assert deployed_version().commit is None


def test_started_at_is_the_injected_instant_in_iso_8601_utc() -> None:
    frozen = datetime(2026, 7, 28, 17, 4, 11, 123456, tzinfo=UTC)
    deployed = deployed_version(now=lambda: frozen)
    # Second precision with an explicit `Z`: the harness parses this back to compare against
    # source-file mtimes, so a bare naive string (ambiguous offset) would be unusable.
    assert deployed.started_at == "2026-07-28T17:04:11Z"
    assert datetime.fromisoformat(deployed.started_at) == frozen.replace(microsecond=0)


def test_started_at_defaults_to_now_and_is_timezone_aware() -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    parsed = datetime.fromisoformat(deployed_version().started_at)
    after = datetime.now(UTC)
    assert parsed.tzinfo is not None
    assert before <= parsed <= after


def test_payload_is_exactly_the_four_documented_keys() -> None:
    # The /readyz `version` object is a published shape (ADR-0482 §1); a silently added or
    # renamed key would change what the preflight and any operator reading the body see.
    assert set(deployed_version().payload()) == {"version", "commit", "is_release", "started_at"}
