"""Authority recovery faults on the installed authority lanes (#3185, ADR-0787).

Gated ``live_stack``. Each arm is dormant unless its lane config is named:
``LOCAL_AUTHORITY_LANE_CONFIG`` for the local authority, ``KDIVE_LIVE_VM_REMOTE_AUTHORITY_CONFIG``
for the remote one. A blocked arm passes its lane gates, records its narrower reason and skips
with ``blocked:`` in the reason; it never reports a pass.
"""

from __future__ import annotations

import asyncio
import os
import platform
import secrets
from collections.abc import Callable
from pathlib import Path

import pytest

from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack import authority_faults
from tests.integration.live_stack.authority_faults import ToolCall
from tests.integration.live_stack.authority_lane import (
    CONFIG_ENV,
    REMOTE_CONFIG_ENV,
    AuthorityBody,
    AuthoritySystem,
    on_authority_system,
)
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.evidence import EvidenceWriter
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.tool_cells import Grants, HttpCaller

pytestmark = pytest.mark.live_stack
# The local arm runs on the control-plane host's architecture; the remote authority lane is x86_64.
ARMS = pytest.mark.parametrize(
    ("provider", "config_env", "arch"),
    [
        ("local-libvirt", CONFIG_ENV, platform.machine()),
        ("remote-libvirt", REMOTE_CONFIG_ENV, "x86_64"),
    ],
    ids=["local", "remote"],
)


def _run(tmp_path: Path, provider: str, arch: str, name: str) -> CellRun:
    cell = Cell(
        name,
        f"tool/{name}",
        3185,
        "authority-faults",
        name,
        ("effect", "cleanup"),
        provider=provider,
        host_arch=arch,
        guest_arch=arch,
    )
    return CellRun(cell, EvidenceWriter(tmp_path))


def _platform_admin(base_url: str, issuer: OidcIssuer) -> ToolCall:
    """A platform admin with a fresh project of its own and no role in the frame's project."""
    caller = HttpCaller("direct", base_url, issuer)
    project = f"cov-{secrets.token_hex(4)}"
    token = caller.token(Grants(f"{project}-platform_admin", (project,), {}, ("platform_admin",)))

    async def call(tool: str, args: dict[str, object]) -> ToolResponse:
        result = await caller.call(tool, args, token)
        assert isinstance(result, ToolResponse), f"{tool} answered {len(result)} envelopes"
        return result

    return call


def _lane(config_env: str) -> tuple[str, str, OidcIssuer]:
    if os.environ.get(config_env) is None:
        pytest.skip(f"{config_env} unset; that installed authority lane is not configured")
    db_url = os.environ.get("KDIVE_DATABASE_URL")
    if not db_url:
        pytest.fail('set KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"')
    return db_url, require_stack(), require_issuer()


def _prove(run: CellRun, config_env: str, arm: Callable[[str, ToolCall], AuthorityBody]) -> None:
    db_url, base_url, issuer = _lane(config_env)
    body = arm(db_url, _platform_admin(base_url, issuer))
    project = f"cov-{secrets.token_hex(4)}"
    try:
        asyncio.run(on_authority_system(run, base_url, issuer, db_url, project=project, body=body))
    except ScenarioStop as stop:
        assert stop.outcome is Outcome.BLOCKED, str(stop)
        pytest.skip(f"blocked: {stop}")
    assert set(run.assertions) == {"effect", "cleanup"}


def _blocked(run: CellRun, config_env: str, reason: str) -> None:
    _db_url, base_url, _issuer = _lane(config_env)
    with pytest.raises(ScenarioStop) as stop:
        authority_faults.blocked_after_gates(run, base_url, reason)
    pytest.skip(f"blocked: {stop.value}")


@ARMS
def test_force_release_after_quiescent_teardown(
    tmp_path: Path, provider: str, config_env: str, arch: str
) -> None:
    def arm(db_url: str, call: ToolCall) -> AuthorityBody:
        async def body(system: AuthoritySystem) -> dict[str, object]:
            return await authority_faults.held_teardown_release(system, db_url, call)

        return body

    _prove(_run(tmp_path, provider, arch, "authority-held-teardown"), config_env, arm)


@ARMS
def test_recovery_orphan_publication_and_disposition(
    tmp_path: Path, provider: str, config_env: str, arch: str
) -> None:
    run = _run(tmp_path, provider, arch, "authority-recovery-orphan")
    if provider == "remote-libvirt":
        _blocked(run, config_env, authority_faults.REMOTE_ORPHAN_BLOCKED)

    def arm(db_url: str, call: ToolCall) -> AuthorityBody:
        async def body(system: AuthoritySystem) -> dict[str, object]:
            return await authority_faults.quarantined_orphan(system, db_url, call)

        return body

    _prove(run, config_env, arm)


@ARMS
def test_recovery_conflict_is_blocked(
    tmp_path: Path, provider: str, config_env: str, arch: str
) -> None:
    run = _run(tmp_path, provider, arch, "authority-recovery-conflict")
    _blocked(run, config_env, authority_faults.CONFLICT_BLOCKED)
