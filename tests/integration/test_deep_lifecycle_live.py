"""Deep lifecycle across representative guests and pinned kernels (#2809, ADR-0715).

``live_stack``-marked. One parameter per local-libvirt ``deep-lifecycle`` contract cell whose
guest architecture is the host's: the family's representative catalog image is provisioned, the
cell's pinned fixture kernel is uploaded, completed, installed and booted over HTTP, and the test
reconnects with the same key, checks the running release and GNU build ID, loads a module from
the upload, then releases and proves owned cleanup. Each parameter writes one version-1
``Evidence`` record under ``KDIVE_ARTIFACT_DIR``; ``docs/operating/runbooks/live-testing.md``
covers fixtures, staging, bindings, assembly and qualification.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET  # noqa: S405 - the worker's own domain XML  # nosec B405
from functools import partial
from pathlib import Path

import pytest

from kdive.images.rootfs.catalog import load_rootfs_catalog
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.deep_lifecycle import (
    FIXTURE_ROOT_ENV,
    baseline,
    deep_body,
    load_fixture,
    native_cells,
    representative,
)
from tests.integration.live_stack.scenario import (
    CellRun,
    ScenarioStop,
    domain_xml,
    on_catalog_system,
    run_cell,
)

pytestmark = pytest.mark.live_stack

_PROJECT = "deep-lifecycle"


def _domain_kernel(system_id: str) -> str:
    """The ``<os><kernel>`` file the domain boots: the install's staged kernel."""
    kernel = ET.fromstring(domain_xml(system_id)).findtext("./os/kernel")  # noqa: S314  # nosec B314
    assert kernel, "the booted domain names no direct kernel"
    return kernel


async def _deep(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str, *, tmp: Path) -> None:
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
    image = representative(run.cell)

    async def body(op: LiveStackClient, system_id: str, owned: list[str]) -> None:
        await deep_body(
            run,
            op,
            system_id,
            owned,
            project=_PROJECT,
            entry=load_rootfs_catalog()[image],
            tree=tree,
            manifest=manifest,
            tmp=tmp,
            staged_kernel=_domain_kernel,
        )

    await on_catalog_system(run, base_url, issuer, db_url, project=_PROJECT, image=image, body=body)
    assert load_fixture(Path(root), baseline(run.cell), arch)[1] == manifest, (
        "the fixture changed during the run"
    )


@pytest.mark.parametrize("cell", native_cells(), ids=lambda c: f"{c.family}-{baseline(c)}")
def test_deep_lifecycle(cell: Cell, tmp_path: Path) -> None:
    """Upload → install → boot → reconnect → build identity → module load → cleanup."""
    run_cell(cell, partial(_deep, tmp=tmp_path))
