"""Report config settings are registered and parse to their defaults (ADR-0208)."""

from __future__ import annotations

import kdive.config as config
from kdive.config.core_settings import (
    REPORT_ARTIFACT_RETENTION_DAYS,
    REPORT_INLINE_MAX_BYTES,
    SETTINGS,
)


def test_report_inline_max_bytes_default() -> None:
    assert config.require(REPORT_INLINE_MAX_BYTES) == 64 * 1024


def test_report_artifact_retention_days_default() -> None:
    assert config.require(REPORT_ARTIFACT_RETENTION_DAYS) == 7


def test_report_settings_registered() -> None:
    names = {s.name for s in config.all_settings()}
    assert "KDIVE_REPORT_INLINE_MAX_BYTES" in names
    assert "KDIVE_REPORT_ARTIFACT_RETENTION_DAYS" in names
    assert REPORT_INLINE_MAX_BYTES in SETTINGS
    assert REPORT_ARTIFACT_RETENTION_DAYS in SETTINGS


def test_report_artifact_retention_is_store_scoped() -> None:
    # The reconciler runs the GC sweep, so it must read this setting.
    assert REPORT_ARTIFACT_RETENTION_DAYS.processes == frozenset({"server", "worker", "reconciler"})
    assert REPORT_ARTIFACT_RETENTION_DAYS.group == "reports"
