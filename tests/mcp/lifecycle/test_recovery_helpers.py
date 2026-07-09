"""Unit tests for the redaction-safe recovery summary helpers (#568, ADR-0180)."""

from __future__ import annotations

from datetime import UTC, datetime

from kdive.mcp.tools.lifecycle._recovery import (
    iso,
    provisioning_profile_summary,
)


def test_iso_serializes_and_passes_through_none() -> None:
    dt = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    assert iso(dt) == dt.isoformat()
    assert iso(None) is None


def test_provisioning_summary_allowlists_only_safe_fields() -> None:
    profile = {
        "arch": "x86_64",
        "boot_method": "direct-kernel",
        "vcpu": 4,
        "memory_mb": 8192,
        "disk_gb": 40,
        "kernel_source_ref": "git@secret",
        "provider": {"local-libvirt": {}},
    }
    summary = provisioning_profile_summary(profile)
    assert summary == {
        "arch": "x86_64",
        "boot_method": "direct-kernel",
        "vcpu": 4,
        "memory_mb": 8192,
        "disk_gb": 40,
    }
    assert "secret" not in str(summary)


def test_provisioning_summary_tolerates_missing_and_wrong_types() -> None:
    # A slightly-off stored document must not raise and must drop bad-typed values.
    assert provisioning_profile_summary({}) == {}
    assert provisioning_profile_summary({"vcpu": "lots", "arch": 7}) == {}
