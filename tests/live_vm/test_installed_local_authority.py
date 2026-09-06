"""Installed local external-boot authority native carrier (#2151).

The normal arm uses only public MCP tools and real worker job polling. Deterministic interruption
is a separate fault arm and does not gate this proof.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.spine import LiveStackClient, mint_role_token
from tests.live_vm.installed_local_authority_support import (
    ResourceLedger,
    await_completed_operations,
    drive_normal_operations,
    load_config,
)


def _output(*argv: str) -> str:
    result = subprocess.run(argv, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.mark.live_vm
def test_installed_local_authority_normal_operations() -> None:
    """Prove installed activate, release, and cleanup through MCP and the fixed worker."""
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

    issuer = require_issuer()
    base_url = require_stack()
    db_url = os.environ.get("KDIVE_DATABASE_URL")
    assert db_url, "native authority carrier requires KDIVE_DATABASE_URL"
    token = mint_role_token(
        issuer,
        project=config.project,
        agent_session=config.ownership_prefix,
        role="admin",
    )
    ledger = ResourceLedger(config.ownership_prefix)

    async def run() -> None:
        client = LiveStackClient.over_http(base_url, token)
        async with client:
            primary: Exception | None = None
            try:
                _investigation_id, run_id = await drive_normal_operations(client, config, ledger)
                await await_completed_operations(
                    db_url, run_id, frozenset({"activate", "release", "cleanup"})
                )
            except Exception as exc:  # preserve the native failure while still attempting cleanup
                primary = exc
            cleanup_failures: list[Exception] = []
            investigations = [r for r in ledger.resources if r.kind == "investigation"]
            for resource in reversed(investigations):
                try:
                    closed = await client.call_tool(
                        "investigations.close", investigation_id=resource.identity
                    )
                    assert not isinstance(closed, list)
                    assert closed.status not in {"error", "failed"}
                except Exception as exc:
                    cleanup_failures.append(exc)
            if primary is not None:
                cleanup_failures.insert(0, primary)
            if len(cleanup_failures) == 1:
                raise cleanup_failures[0]
            if cleanup_failures:
                raise ExceptionGroup("native carrier and cleanup failures", cleanup_failures)

    asyncio.run(run())
