"""Check ownership or qualify candidate evidence against the shipped coverage contract."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.coverage_campaign.contract import build_contract
from scripts.coverage_campaign.evidence import EvidenceError, read_bindings, read_results
from scripts.coverage_campaign.results import merge_and_render, qualify


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "check", help="check inventory ownership; does not prove live qualification"
    )
    qualification = commands.add_parser("qualify", help="require matching evidence for every cell")
    qualification.add_argument("--inputs", type=Path, required=True)
    qualification.add_argument("--results", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        contract = build_contract()
    except ValueError, OSError, SyntaxError:
        print(
            "invalid-contract: reconcile obligations.toml with registry, catalog and test nodes",
            file=sys.stderr,
        )
        return 2
    if args.command == "check":
        pending = sum(cell.node_id is None for cell in contract.cells)
        print(
            f"Coverage mapping valid: {len(contract.inventory.tools)} tools, "
            f"{len(contract.inventory.images)} catalog images, "
            f"{len(contract.cells)} required cells; "
            f"{pending} pending; live qualification unproven.\nMatrix: {contract.matrix_sha256}"
        )
        return 0
    try:
        inputs, results = read_bindings(args.inputs), read_results(args.results)
    except EvidenceError as error:
        print(error, file=sys.stderr)
        return 2
    report = qualify(contract, inputs, results)
    print(merge_and_render(report), end="")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
