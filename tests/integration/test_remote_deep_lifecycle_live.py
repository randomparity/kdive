"""Remote deep lifecycle on a separate provider host (#2810, ADR-0715).

``live_stack``-marked. One parameter per ``deep-lifecycle/remote-libvirt/x86_64`` contract cell:
the family's remote base image is provisioned on the ``remote-libvirt`` host, the cell's pinned
fixture kernel is uploaded, completed, installed in-guest and booted over HTTP, and the test
reconnects with the same key, checks the running release and GNU build ID, reads the installed
kernel's digest in the guest, loads a module from the upload, then releases and proves on the
provider host that the domain, its volumes and the ``kdive-*`` domain set are reclaimed. SUSE
records ``blocked`` (#3082); Debian requires its rebuilt family helper and guest return route.
Each parameter writes one version-1 ``Evidence``
record under ``KDIVE_ARTIFACT_DIR``; ``docs/operating/runbooks/remote-live-stack.md`` §7 covers
the topology, observer access, bindings, assembly and qualification.
"""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path

import pytest

from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.deep_lifecycle import (
    FIXTURE_ROOT_ENV,
    baseline,
    deep_body,
    load_fixture,
)
from tests.integration.live_stack.remote_lifecycle import (
    REMOTE_BLOCKED,
    REMOTE_REPRESENTATIVES,
    guest_boot_kernel,
    on_remote_system,
    remote_cells,
)
from tests.integration.live_stack.scenario import CellRun, ScenarioStop, run_cell

pytestmark = pytest.mark.live_stack

_PROJECT = "remote-deep-lifecycle"


async def _deep(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str, *, tmp: Path) -> None:
    family = str(run.cell.family)
    if family in REMOTE_BLOCKED:
        raise ScenarioStop(Outcome.BLOCKED, REMOTE_BLOCKED[family])
    root = os.environ.get(FIXTURE_ROOT_ENV)
    if not root:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{FIXTURE_ROOT_ENV} unset; build the pinned kernel fixtures"
        )
    arch = str(run.cell.guest_arch)
    try:
        tree, manifest = load_fixture(Path(root), baseline(run.cell), arch)
    except ValueError as exc:
        raise ScenarioStop(Outcome.BLOCKED, f"{baseline(run.cell)} fixture: {exc}") from None

    async def body(op: LiveStackClient, system_id: str, owned: list[str]) -> None:
        await deep_body(
            run,
            op,
            system_id,
            owned,
            project=_PROJECT,
            entry=REMOTE_REPRESENTATIVES[family],
            tree=tree,
            manifest=manifest,
            tmp=tmp,
            installed_kernel=partial(guest_boot_kernel, family=family),
        )

    await on_remote_system(
        run, base_url, issuer, db_url, project=_PROJECT, family=family, body=body
    )
    assert load_fixture(Path(root), baseline(run.cell), arch)[1] == manifest, (
        "the fixture changed during the run"
    )


@pytest.mark.parametrize("cell", remote_cells(), ids=lambda c: f"{c.family}-{baseline(c)}")
def test_remote_deep_lifecycle(cell: Cell, tmp_path: Path) -> None:
    """Upload → install → boot → reconnect → build identity → module → remote-side cleanup."""
    run_cell(cell, partial(_deep, tmp=tmp_path))
