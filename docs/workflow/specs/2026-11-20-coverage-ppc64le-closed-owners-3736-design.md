# Coverage contract: ppc64le cells of closed owners go to the POWER lane (#3736)

Follows #3718, which re-homed the hot-plug ppc64le cells to #2818 (open, the POWER lane).

## Problem

Two groups of ppc64le cells still name closed issues as owner, so no open issue tracks them:

- 32 `ops.recover_stranded_activation` cells owned by #3552: 3552 is missing from the ppc64le
  re-homing tuple in `_tool_cells`.
- 6 CLI-exposure cells (`accounting.usage`, `allocations.release`, `reports.generate`) owned by
  #3099: the `exposure == "cli"` assignment runs after the ppc64le re-homing and overwrites it.

## Scope

In: `scripts/coverage_campaign/contract.py` owner mapping; `tests/scripts/test_coverage_contract.py`.
Out: closed owners of non-ppc64le cells (#3099's 454 other cells, #3552's 32 x86_64 cells keep
their owners); a general closed-owner check in the contract.

## Contract

- 3552 joins the ppc64le tuple, so its ppc64le cells are owned by #2818.
- The CLI owner assignment yields #2818 for a ppc64le cell and #3099 otherwise.
- No other cell changes owner. `matrix_sha256` changes because cell owners are hashed; no
  committed fixture holds it, so nothing is regenerated.

## Tests

- New: ppc64le cells of both groups are owned by #2818 and their x86_64 counterparts by #3552 and
  #3099; no ppc64le cell is owned by #3552 or #3099.
- `test_cli_cells_twin_the_core_direct_cells` expects #2818 for the ppc64le CLI cells.
- Both fail on main's mapping and pass with the change.

## Failure model

A wrong mapping only mislabels which issue a pending cell reports to; qualification of the cell
itself is unchanged. Rollback is a revert.
