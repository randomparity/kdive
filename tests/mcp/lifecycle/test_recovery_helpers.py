"""Unit tests for the redaction-safe recovery summary helpers (#568, ADR-0180)."""

from __future__ import annotations

from datetime import UTC, datetime

from kdive.mcp.tools.lifecycle._recovery import (
    build_profile_summary,
    iso,
    provisioning_profile_summary,
)


def test_iso_serializes_and_passes_through_none() -> None:
    dt = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    assert iso(dt) == dt.isoformat()
    assert iso(None) is None


def test_build_summary_git_provenance_omits_remote() -> None:
    profile = {
        "source": "server",
        "build_host": "build-1",
        "kernel_source_ref": {"git": {"remote": "https://h/r", "ref": "main"}},
    }
    summary = build_profile_summary(profile)
    assert summary == {
        "build_source": "server",
        "build_host": "build-1",
        "build_source_provenance": "git",
    }
    assert "h/r" not in str(summary)


def test_build_summary_warm_tree_and_default_source() -> None:
    summary = build_profile_summary({"kernel_source_ref": "REPLACE_ME-warm-tree"})
    assert summary["build_source"] == "server"
    assert summary["build_source_provenance"] == "warm-tree"
    assert "build_host" not in summary


def test_build_summary_external_provenance() -> None:
    summary = build_profile_summary({"source": "external"})
    assert summary["build_source"] == "external"
    assert summary["build_source_provenance"] == "external"


def test_build_summary_mapping_ref_without_git_is_warm_tree() -> None:
    # A Mapping kernel_source_ref that lacks a "git" key is warm-tree, NOT git: provenance is
    # "git" only when the mapping actually carries a git block (both the Mapping check AND the
    # "git" membership must hold).
    summary = build_profile_summary(
        {"source": "server", "kernel_source_ref": {"warm_tree": {"path": "/srv/tree"}}}
    )
    assert summary["build_source_provenance"] == "warm-tree"


def test_build_summary_none_ref_falls_back_to_warm_tree() -> None:
    # A missing/None kernel_source_ref must derive warm-tree without raising (the Mapping
    # guard short-circuits before any membership test on a non-mapping).
    summary = build_profile_summary({"source": "server"})
    assert summary["build_source_provenance"] == "warm-tree"


def test_build_summary_non_string_source_falls_back_to_server() -> None:
    # A stored document with a non-string source must surface the literal "server" lane, not
    # a mangled fallback token.
    summary = build_profile_summary({"source": 123})
    assert summary["build_source"] == "server"


def test_provisioning_summary_allowlists_only_safe_fields() -> None:
    profile = {
        "arch": "x86_64",
        "boot_method": "direct-kernel",
        "vcpu": 4,
        "memory_mb": 8192,
        "disk_gb": 40,
        "kernel_source_ref": "git@secret",
        "provider": {"local-libvirt": {"ssh_credential_ref": "file:///run/secret"}},
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
