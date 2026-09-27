"""Complete expected-cell accounting and fail-closed qualification (ADR-0686)."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from scripts.coverage_campaign.contract import Cell, Contract, Inventory, digest, image_family
from scripts.coverage_campaign.evidence import Context, Evidence, InputBindings, Outcome


@dataclass(frozen=True)
class CellVerdict:
    cell: Cell
    outcome: Outcome
    qualified: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Qualification:
    candidate_sha: str
    matrix_sha256: str
    cells: tuple[CellVerdict, ...]
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return bool(self.cells) and not self.errors and all(cell.qualified for cell in self.cells)


def _context_errors(
    cell: Cell, expected: Context, actual: Context, inventory: Inventory
) -> list[str]:
    errors = []
    if actual != expected:
        errors.append("input-context-mismatch")
    if cell.host_arch is not None and expected.host_arch != cell.host_arch:
        errors.append("host-architecture-mismatch")
    if cell.guest_arch is not None and (
        expected.guest_arch != cell.guest_arch or expected.guest_os is None
    ):
        errors.append("guest-identity-mismatch")
    if expected.accelerator != cell.accelerator:
        errors.append("accelerator-mismatch")
    if any(getattr(expected, name, None) is None for name in cell.inputs):
        errors.append("required-input-missing")
    if cell.image is not None:
        image = inventory.images[cell.image]
        if expected.guest_os != f"{image.distro}:{image.version}":
            errors.append("catalog-platform-mismatch")
    if cell.family is not None:
        platform = expected.host_os if cell.operation == "host-install" else expected.guest_os
        distro = platform.split(":", 1)[0] if platform else None
        families = {
            image_family(image) for image in inventory.images.values() if image.distro == distro
        }
        if cell.family not in families:
            errors.append("platform-family-mismatch")
    return errors


def _identity_errors(
    cell: Cell, inputs: InputBindings, result: Evidence, context: Context, inventory: Inventory
) -> list[str]:
    errors = []
    if result.scenario_id != cell.scenario_id or result.node_id != cell.node_id:
        errors.append("scenario-identity-mismatch")
    if result.candidate_sha != inputs.candidate_sha:
        errors.append("candidate-mismatch")
    if result.matrix_sha256 != inputs.matrix_sha256:
        errors.append("matrix-mismatch")
    if result.input_sha256 != digest(context):
        errors.append("input-digest-mismatch")
    if set(cell.roles) - result.deployed_roles.keys():
        errors.append("deployed-role-missing")
    if any(sha != inputs.candidate_sha for sha in result.deployed_roles.values()):
        errors.append("deployed-revision-mismatch")
    errors.extend(_context_errors(cell, context, result.context, inventory))
    return errors


def _cell_verdict(
    cell: Cell, inputs: InputBindings, records: list[Evidence], inventory: Inventory
) -> CellVerdict:
    if not records:
        reasons = (
            ("missing-result",) if cell.node_id else ("pending-implementation", "missing-result")
        )
        return CellVerdict(cell, Outcome.NOT_RUN, False, reasons)
    if len(records) != 1:
        return CellVerdict(cell, Outcome.FAILURE, False, ("duplicate-result",))
    if cell.node_id is None:
        return CellVerdict(cell, Outcome.NOT_RUN, False, ("pending-implementation",))
    context = inputs.cells.get(cell.id)
    if context is None:
        return CellVerdict(cell, Outcome.BLOCKED, False, ("input-binding-missing",))
    result = records[0]
    errors = _identity_errors(cell, inputs, result, context, inventory)
    outcome = result.outcome
    if "known-defect" in result.impediments:
        errors.append("known-defect")
        outcome = Outcome.FAILURE
    if "missing-prerequisite" in result.impediments:
        errors.append("missing-prerequisite")
        outcome = Outcome.BLOCKED if outcome is not Outcome.FAILURE else outcome
    if outcome in (Outcome.FAILURE, Outcome.BLOCKED, Outcome.NOT_RUN):
        return CellVerdict(cell, outcome, False, tuple(errors or ["reported-" + outcome.value]))
    expected = {
        "functional": Outcome.SUCCESS,
        "rejection": Outcome.REJECTION,
        "unsupported": Outcome.UNSUPPORTED,
    }[cell.kind]
    if outcome != expected:
        errors.append("outcome-kind-mismatch")
    if outcome is Outcome.UNSUPPORTED:
        if not cell.unsupported_reason:
            errors.append("unsupported-without-reviewed-rule")
    else:
        if set(result.assertions) != set(cell.assertions):
            errors.append("required-assertions-mismatch")
        if not result.assertions or set(result.assertions.values()) - set(result.artifacts):
            errors.append("assertion-artifact-missing")
    return CellVerdict(cell, Outcome.FAILURE if errors else outcome, not errors, tuple(errors))


def qualify(contract: Contract, inputs: InputBindings, results: list[Evidence]) -> Qualification:
    expected = {cell.id for cell in contract.cells}
    errors = []
    if not expected:
        errors.append("empty-contract")
    if len(expected) != len(contract.cells):
        errors.append("duplicate-contract-cell")
    if contract.version != inputs.version:
        errors.append("contract-version-mismatch")
    if contract.matrix_sha256 != inputs.matrix_sha256:
        errors.append("matrix-mismatch")
    if inputs.cells.keys() - expected:
        errors.append("unexpected-input-binding")
    by_cell: dict[str, list[Evidence]] = defaultdict(list)
    for result in results:
        if result.cell_id not in expected:
            errors.append("unexpected-result")
        by_cell[result.cell_id].append(result)
    verdicts = tuple(
        _cell_verdict(cell, inputs, by_cell[cell.id], contract.inventory)
        for cell in sorted(contract.cells, key=lambda cell: cell.id)
    )
    return Qualification(
        inputs.candidate_sha, contract.matrix_sha256, verdicts, tuple(sorted(set(errors)))
    )


def merge_and_render(report: Qualification) -> str:
    counts = Counter(cell.outcome for cell in report.cells)
    lines = [
        f"Qualification: {'PASS' if report.passed else 'FAIL'}",
        f"Candidate: {report.candidate_sha}",
        f"Matrix: {report.matrix_sha256}",
        "Outcomes: " + ", ".join(f"{outcome.value}={counts[outcome]}" for outcome in Outcome),
        "Errors: " + (", ".join(report.errors) or "none"),
        "",
        "| Required cell | Owner | Outcome | Qualified | Reasons |",
        "|---|---|---|---|---|",
    ]
    for verdict in report.cells:
        lines.append(
            f"| `{verdict.cell.id}` | #{verdict.cell.owner} | {verdict.outcome.value} | "
            f"{'yes' if verdict.qualified else 'no'} | {', '.join(verdict.reasons) or 'none'} |"
        )
    return "\n".join(lines) + "\n"
