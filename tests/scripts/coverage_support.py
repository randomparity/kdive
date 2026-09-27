"""Synthetic complete evidence shared by coverage qualification tests."""

from __future__ import annotations

from scripts.coverage_campaign.contract import Cell, Contract, Inventory, digest
from scripts.coverage_campaign.evidence import Context, Evidence, InputBindings, Outcome

SHA = "a" * 40
DIGEST = "b" * 64
NODE = "tests/scripts/test_results.py::test_complete_independent_evidence_qualifies"


def complete_evidence() -> tuple[Contract, InputBindings, list[Evidence]]:
    cells = tuple(
        Cell(
            id=f"example/{kind}",
            scenario_id=f"example/{kind}",
            node_id=NODE,
            owner=2804,
            operation="example",
            observation="Verify terminal effect and cleanup.",
            assertions=("effect", "terminal", "cleanup"),
            kind=kind,
            roles=("server", "worker", "reconciler", "authority"),
            host_arch="x86_64",
            guest_arch="x86_64",
            accelerator="kvm",
            inputs=(
                "image_sha256",
                "kernel_sha256",
                "kernel_source_sha",
                "kernel_config_sha256",
                "compiler_id",
                "kernel_build_id",
            ),
            unsupported_reason="Reviewed capability rule" if kind == "unsupported" else None,
        )
        for kind in ("functional", "rejection", "unsupported")
    )
    contract = Contract(1, DIGEST, cells, Inventory((), {}, frozenset(), {}))
    context = Context(
        host_os="ubuntu:26.04",
        host_arch="x86_64",
        guest_os="fedora:44",
        guest_arch="x86_64",
        accelerator="kvm",
        image_sha256=DIGEST,
        kernel_sha256=DIGEST,
        kernel_source_sha=SHA,
        kernel_config_sha256=DIGEST,
        compiler_id=DIGEST,
        kernel_build_id="c" * 40,
    )
    bindings = InputBindings(
        version=1, candidate_sha=SHA, matrix_sha256=DIGEST, cells={c.id: context for c in cells}
    )
    outcomes = (Outcome.SUCCESS, Outcome.REJECTION, Outcome.UNSUPPORTED)
    results = [
        Evidence(
            version=1,
            cell_id=cell.id,
            scenario_id=cell.scenario_id,
            node_id=NODE,
            outcome=outcome,
            candidate_sha=SHA,
            matrix_sha256=DIGEST,
            input_sha256=digest(context),
            deployed_roles={"server": SHA, "worker": SHA, "reconciler": SHA, "authority": SHA},
            context=context,
            duration_seconds=1.0,
            assertions={name: DIGEST for name in cell.assertions},
            artifacts=[DIGEST],
            impediments=[],
        )
        for cell, outcome in zip(cells, outcomes, strict=True)
    ]
    return contract, bindings, results
