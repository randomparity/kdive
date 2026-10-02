"""Unit tests for the shared cell frame and the deep-lifecycle inputs (#2809); no stack."""

from __future__ import annotations

from pathlib import Path

from scripts.coverage_campaign.contract import Cell
from tests.integration.live_stack.evidence import EvidenceWriter, RunIdentity
from tests.integration.live_stack.scenario import CellRun

_IDENTITY = RunIdentity(
    candidate_sha="a" * 40,
    matrix_sha256="b" * 64,
    host_os="ubuntu:26.04",
    host_arch="x86_64",
    clean=True,
    deployed_roles={},
)


def test_cell_run_context_merges_observed(tmp_path: Path) -> None:
    cell = Cell("c", "s", 1, "op", "obs", ("effect",))
    run = CellRun(cell, EvidenceWriter(tmp_path))
    assert run.context(_IDENTITY).accelerator == "none"
    run.observed |= {"guest_os": "fedora:44", "guest_arch": "x86_64", "accelerator": "kvm"}
    context = run.context(_IDENTITY)
    assert (context.host_os, context.host_arch) == ("ubuntu:26.04", "x86_64")
    assert (context.guest_os, context.accelerator) == ("fedora:44", "kvm")
