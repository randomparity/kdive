"""Installed local external-boot authority native carrier (#2151).

This module intentionally stops before provider mutation until the installed runtime supplies the
deterministic provider-effect barrier required by the takeover, restart, and journal-loss arms.
Unit tests of its helpers are orchestration evidence only, never native acceptance.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.live_vm.installed_local_authority_support import load_config, require_fault_barrier


def _output(*argv: str) -> str:
    result = subprocess.run(argv, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.mark.live_vm
def test_installed_local_authority_native_operations() -> None:
    """Gate the real six-operation carrier on coherent install and deterministic fault control."""
    config = load_config()
    if config is None:
        pytest.skip("installed local-authority carrier is not configured")

    installed = _output("sudo", "-n", "cat", "/opt/kdive-provider-authority/revision")
    assert installed == config.installed_revision, (
        f"installed authority revision {installed!r} does not match configured coherent revision"
    )
    assert _output("systemctl", "is-active", config.authority_service) == "active"
    running_workers = _output(
        "systemctl",
        "list-units",
        "kdive-live-worker@*.service",
        "--state=running",
        "--no-legend",
    )
    assert running_workers, "native authority carrier requires an active fixed worker incarnation"

    # This is a hard prerequisite, not a placeholder pass. Once the assembled runtime exposes the
    # barrier, the carrier can deterministically drive and interrupt the six job operations without
    # turning timing luck or a fake provider into acceptance evidence.
    require_fault_barrier(config)
    pytest.fail(
        "installed provider-effect barrier exists but six-operation orchestration is not bound; "
        "complete the public worker/MCP driver before scheduling native mutation"
    )
