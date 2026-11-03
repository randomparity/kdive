"""Broken/fixed kernel corpus on the local lifecycle (#2817).

``live_stack``-marked. One parameter per ``kernel-corpus`` contract cell whose guest architecture
is the host's. Both fixtures of the cell's case are verified and must share one configuration;
the Fedora representative is provisioned, the cell's revision is uploaded, installed with the
case's reproducer and booted over HTTP, and the case's outcome check judges the boot. Each
parameter writes one version-1 ``Evidence`` record under ``KDIVE_ARTIFACT_DIR``;
``docs/operating/runbooks/live-testing.md`` covers fixtures, bindings and qualification.
"""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from kdive.images.rootfs.catalog import load_rootfs_catalog
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.deep_lifecycle import FIXTURE_ROOT_ENV, load_fixture
from tests.integration.live_stack.kernel_corpus import (
    CASES,
    IMAGE,
    case_of,
    corpus_body,
    native_cells,
    revision,
    same_config,
    selection,
)
from tests.integration.live_stack.resource_limits import domain_kernel
from tests.integration.live_stack.scenario import CellRun, ScenarioStop, on_catalog_system, run_cell

pytestmark = pytest.mark.live_stack

_PROJECT = "kernel-corpus"


def _fixtures(root: Path, case: str, arch: str) -> dict[str, tuple[Path, dict[str, Any]]]:
    """Both verified revisions of ``case``; a missing or rejected one blocks the cell."""
    loaded = {}
    for rev in ("broken", "fixed"):
        try:
            loaded[rev] = load_fixture(root, selection(case, rev), arch)
        except ValueError as exc:
            raise ScenarioStop(Outcome.BLOCKED, f"{selection(case, rev)} fixture: {exc}") from None
    return loaded


async def _corpus(
    run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str, *, tmp: Path
) -> None:
    root = os.environ.get(FIXTURE_ROOT_ENV)
    if not root:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{FIXTURE_ROOT_ENV} unset; build the kernel corpus fixtures"
        )
    case, rev = case_of(run.cell), revision(run.cell)
    fixtures = _fixtures(Path(root), case, str(run.cell.guest_arch))
    shared = same_config({name: manifest for name, (_, manifest) in fixtures.items()})
    run.prove(
        "same-reproducer-config",
        {
            "case": case,
            "reproducer": {
                "cmdline": CASES[case].cmdline,
                "expected_boot_failure": CASES[case].expected_boot_failure,
                "probe": CASES[case].probe,
            },
            "config_sha256": shared,
            "revisions": {name: m["source"]["commit"] for name, (_, m) in fixtures.items()},
        },
    )
    tree, manifest = fixtures[rev]

    async def body(op: LiveStackClient, system_id: str, owned: list[str]) -> None:
        await corpus_body(
            run,
            op,
            system_id,
            owned,
            project=_PROJECT,
            entry=load_rootfs_catalog()[IMAGE],
            tree=tree,
            manifest=manifest,
            tmp=tmp,
            installed_kernel=domain_kernel,
        )

    await on_catalog_system(run, base_url, issuer, db_url, project=_PROJECT, image=IMAGE, body=body)
    assert load_fixture(Path(root), selection(case, rev), manifest["arch"])[1] == manifest, (
        "the fixture changed during the run"
    )


@pytest.mark.parametrize("cell", native_cells(), ids=lambda c: f"{case_of(c)}-{revision(c)}")
def test_kernel_corpus(cell: Cell, tmp_path: Path) -> None:
    """Same reproducer and config on both revisions → the case's broken or fixed outcome."""
    run_cell(cell, partial(_corpus, tmp=tmp_path))
