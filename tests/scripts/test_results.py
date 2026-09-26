from __future__ import annotations

from dataclasses import replace

import pytest

from scripts.coverage_campaign.contract import Cell, Contract, Inventory, digest
from scripts.coverage_campaign.evidence import Context, Evidence, InputBindings, Outcome
from scripts.coverage_campaign.results import merge_and_render, qualify

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


def test_complete_independent_evidence_qualifies() -> None:
    contract, bindings, results = complete_evidence()
    report = qualify(contract, bindings, results)
    assert report.passed
    assert {v.outcome for v in report.cells} == {
        Outcome.SUCCESS,
        Outcome.REJECTION,
        Outcome.UNSUPPORTED,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "skip",
        "blocked",
        "failed",
        "stale-image",
        "stale-kernel",
        "candidate",
        "matrix",
        "input-digest",
        "scenario",
        "node",
        "host-arch",
        "guest-arch",
        "accelerator",
        "terminal",
        "cleanup",
        "artifact",
        "role-missing",
        "input-missing",
        "duplicate",
        "unexpected",
        "known-defect",
        "missing-prerequisite",
        "invented-exemption",
        "rejection-substitution",
        "server",
        "worker",
        "reconciler",
        "authority",
    ],
)
def test_each_controlled_fault_turns_complete_qualification_red(mutation: str) -> None:
    contract, bindings, results = complete_evidence()
    assert qualify(contract, bindings, results).passed
    record = results[0].model_dump(mode="json")
    match mutation:
        case "missing":
            results.pop(0)
        case "skip" | "blocked" | "failed":
            record["outcome"] = {"skip": "not-run", "blocked": "blocked", "failed": "failure"}[
                mutation
            ]
        case "stale-image" | "stale-kernel":
            record["context"]["image_sha256" if mutation == "stale-image" else "kernel_sha256"] = (
                "d" * 64
            )
        case "candidate" | "matrix" | "input-digest":
            key = {
                "candidate": "candidate_sha",
                "matrix": "matrix_sha256",
                "input-digest": "input_sha256",
            }[mutation]
            record[key] = "d" * (40 if mutation == "candidate" else 64)
        case "scenario" | "node":
            record["scenario_id" if mutation == "scenario" else "node_id"] += "_different"
        case "host-arch" | "guest-arch":
            record["context"][mutation.replace("-", "_")] = "ppc64le"
        case "accelerator":
            record["context"]["accelerator"] = "tcg"
        case "terminal" | "cleanup":
            record["assertions"].pop(mutation)
        case "artifact":
            record["artifacts"] = []
        case "role-missing":
            record["deployed_roles"].pop("worker")
        case "input-missing":
            record["context"]["compiler_id"] = None
        case "duplicate":
            results.append(results[0])
        case "unexpected":
            results.append(results[0].model_copy(update={"cell_id": "unexpected"}))
        case "known-defect" | "missing-prerequisite":
            record["impediments"] = [mutation]
        case "invented-exemption":
            record["outcome"] = "unsupported"
        case "rejection-substitution":
            record["outcome"] = "rejection"
        case _:
            record["deployed_roles"][mutation] = "d" * 40
    if mutation != "missing":
        results[0] = Evidence.model_validate(record)
    report = qualify(contract, bindings, results)
    assert not report.passed, mutation
    assert len(report.cells) == len(contract.cells)


def test_pending_cell_cannot_be_qualified_by_fabricated_success() -> None:
    contract, bindings, results = complete_evidence()
    contract = replace(
        contract, cells=(replace(contract.cells[0], node_id=None), *contract.cells[1:])
    )
    report = qualify(contract, bindings, results)
    assert not report.passed
    assert report.cells[0].outcome is Outcome.NOT_RUN
    assert "pending-implementation" in report.cells[0].reasons


@pytest.mark.parametrize(
    "fault", ["empty", "missing-binding", "extra-binding", "matrix", "platform", "missing-input"]
)
def test_expected_set_and_independent_bindings_are_mandatory(fault: str) -> None:
    contract, bindings, results = complete_evidence()
    if fault == "empty":
        contract = replace(contract, cells=())
    elif fault == "missing-binding":
        bindings.cells.pop(results[0].cell_id)
    elif fault == "extra-binding":
        bindings.cells["unexpected"] = results[0].context
    elif fault == "matrix":
        bindings = bindings.model_copy(update={"matrix_sha256": "d" * 64})
    else:
        context = results[0].context.model_copy(
            update={
                "accelerator" if fault == "platform" else "compiler_id": "tcg"
                if fault == "platform"
                else None,
            }
        )
        bindings.cells[results[0].cell_id] = context
        results[0] = results[0].model_copy(
            update={"context": context, "input_sha256": digest(context)}
        )
    assert not qualify(contract, bindings, results).passed


@pytest.mark.parametrize("impediment", ["known-defect", "missing-prerequisite"])
def test_exemptions_and_rejections_cannot_hide_defects_or_absent_prerequisites(
    impediment: str,
) -> None:
    contract, bindings, results = complete_evidence()
    for index in (1, 2):
        changed = list(results)
        changed[index] = changed[index].model_copy(update={"impediments": [impediment]})
        assert not qualify(contract, bindings, changed).passed


def test_report_is_complete_stable_and_omits_untrusted_record_labels() -> None:
    contract, bindings, results = complete_evidence()
    results.append(results[0].model_copy(update={"cell_id": "private.internal"}))
    report = qualify(contract, bindings, results[1:])
    text = merge_and_render(report)
    assert "private.internal" not in text
    assert "unexpected-result" in text
    assert "not-run" in text
    assert all(c.id in text for c in contract.cells)
    assert text == merge_and_render(qualify(contract, bindings, list(reversed(results[1:]))))


@pytest.mark.parametrize(
    "operation,family,platform_field,valid,wrong",
    [
        ("image-smoke", "fedora", "guest_os", "fedora:43", "debian:13"),
        ("image-smoke", "fedora", "guest_os", "fedora:43", "fedora:44"),
        ("deep-lifecycle", "fedora", "guest_os", "fedora:44", "rocky:10"),
        ("host-install", "debian", "host_os", "ubuntu:26.04", "fedora:44"),
    ],
)
def test_both_input_files_cannot_redefine_the_required_platform_lane(
    operation: str,
    family: str,
    platform_field: str,
    valid: str,
    wrong: str,
) -> None:
    from scripts.coverage_campaign.contract import build_contract

    actual = build_contract()
    cell = next(
        c
        for c in actual.cells
        if c.operation == operation
        and c.family == family
        and c.guest_arch == "x86_64"
        and (c.image is None or c.image == "fedora-kdive-ready-43")
    )
    cell = replace(cell, node_id=NODE)
    contract = replace(actual, cells=(cell,))
    _, bindings, results = complete_evidence()
    for platform, should_pass in [(valid, True), (wrong, False)]:
        context = results[0].context.model_copy(update={platform_field: platform})
        inputs = bindings.model_copy(
            update={"matrix_sha256": contract.matrix_sha256, "cells": {cell.id: context}}
        )
        result = results[0].model_copy(
            update={
                "cell_id": cell.id,
                "scenario_id": cell.scenario_id,
                "matrix_sha256": contract.matrix_sha256,
                "context": context,
                "input_sha256": digest(context),
                "assertions": {key: DIGEST for key in cell.assertions},
            }
        )
        assert qualify(contract, inputs, [result]).passed is should_pass
