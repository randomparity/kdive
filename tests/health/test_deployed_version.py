"""The deployed-build fact `/readyz` reports (ADR-0482 decision 1).

`deployed_version()` must mirror `version_info()` (ADR-0041/0370) rather than open a fourth
resolution path, and must stamp the process start instant the skew preflight compares against.
"""

from __future__ import annotations

from datetime import UTC, datetime

from kdive.health.deployed_version import deployed_version
from kdive.version import version_info


def test_mirrors_version_info_rather_than_resolving_independently() -> None:
    # ADR-0482 §1: the aux listener adds no fourth version-resolution path. Whatever
    # version_info() says (baked _buildinfo, else live git, else unknown) is what /readyz says.
    info = version_info()
    deployed = deployed_version()
    assert deployed.version == info.version
    assert deployed.commit == info.commit
    assert deployed.is_release == info.is_release


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
