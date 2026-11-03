"""The authority-owned System frame on the installed authority lanes (#3184, #3209).

Gated ``live_stack``. Each arm is dormant unless its lane config is named:
``LOCAL_AUTHORITY_LANE_CONFIG`` for the local authority, ``KDIVE_LIVE_VM_REMOTE_AUTHORITY_CONFIG``
for the remote one. One body opens an Investigation and passes, one raises. Each must leave its
authority's domains, private storage and capacity as it found them.
"""

from __future__ import annotations

import asyncio
import os
import platform
import secrets
from pathlib import Path

import pytest

from scripts.coverage_campaign.contract import Cell
from tests.integration.live_stack.authority_lane import (
    CONFIG_ENV,
    REMOTE_CONFIG_ENV,
    AuthorityBody,
    AuthoritySystem,
    on_authority_system,
)
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.evidence import EvidenceWriter
from tests.integration.live_stack.scenario import CellRun
from tests.integration.live_stack.spine import ok, scalar
from tests.live_vm.installed_local_authority_support import OwnedResource

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


class _BodyFailed(Exception):
    pass


def _run(tmp_path: Path, provider: str, arch: str) -> CellRun:
    cell = Cell(
        "authority-frame-self-test",
        "tool/authority-frame-self-test",
        3184,
        "authority-frame",
        "frame self-test",
        ("effect", "cleanup"),
        provider=provider,
        host_arch=arch,
        guest_arch=arch,
    )
    return CellRun(cell, EvidenceWriter(tmp_path))


def _frame(run: CellRun, body: AuthorityBody, config_env: str) -> None:
    if os.environ.get(config_env) is None:
        pytest.skip(f"{config_env} unset; that installed authority lane is not configured")
    db_url = os.environ.get("KDIVE_DATABASE_URL")
    if not db_url:
        pytest.fail('set KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL"')
    base_url, issuer = require_stack(), require_issuer()
    project = f"cov-{secrets.token_hex(4)}"
    asyncio.run(on_authority_system(run, base_url, issuer, db_url, project=project, body=body))


@ARMS
def test_authority_frame_success(tmp_path: Path, provider: str, config_env: str, arch: str) -> None:
    async def body(system: AuthoritySystem) -> dict[str, object]:
        opened = await scalar(
            system.op,
            "investigations.open",
            project=system.project,
            title=system.config.ownership_prefix,
        )
        investigation = ok(opened, "open-investigation").object_id
        # Left open on purpose: the frame closes every ledger Investigation still open.
        system.ledger.record(OwnedResource(kind="investigation", identity=investigation))
        return {"investigation": "opened"}

    run = _run(tmp_path, provider, arch)
    _frame(run, body, config_env)
    assert set(run.assertions) == {"effect", "cleanup"}
    assert run.observed["accelerator"] == "kvm"


@ARMS
def test_authority_frame_body_failure(
    tmp_path: Path, provider: str, config_env: str, arch: str
) -> None:
    async def body(_system: AuthoritySystem) -> dict[str, object]:
        raise _BodyFailed

    run = _run(tmp_path, provider, arch)
    with pytest.raises(_BodyFailed):
        _frame(run, body, config_env)
    assert "cleanup" not in run.assertions
    assert len(run.artifacts) == 1
