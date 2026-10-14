# Mutating operator tool cells: implementation plan (#3110)

**Goal:** the 216 owner-3110 service cells run over HTTP, each restoring what it changed, and
qualify on the installed local-authority lane in both configurations.

**Architecture:** one carrier, `tests/integration/test_operator_mutation_tool_cells_live.py`, on
the merged tool-cell harness, with the authority-owned System frame for the two authority tools
and the catalog System frame for `images.prune_expired`. The catalog carrier's private-image
helpers move to `tests/integration/live_stack/private_images.py`. Spec:
`docs/workflow/specs/2026-10-14-operator-mutation-tool-cells-design.md`.

**Tech stack:** Python 3.14, pytest, psycopg 3, boto3, fastmcp client.

Expected implementation size: 1150–1350 changed lines (L) — about 120 moved helper lines (net
~40), 950 carrier lines, 60 binding lines, 15 contract-test lines and 70 runbook lines.

## Global Constraints

- Test harness and docs only: no change under `src/`, no migration, no ADR, no deployment file.
- `obligations.toml`: `[implementations]` lines only; keep other PRs' landed lines.
- Evidence and assertion messages carry no host names, addresses or credential paths.
- Every restore goes through a tool; direct SQL writes are limited to ADR-0769's seeding grants.
- Ruff line length 100; `just lint`, `just type` green; run gates bare; multi-line shell under
  `bash`.
- Doc style: no "critical", "robust", "comprehensive", "elegant".

## File map

| File | Change |
|------|--------|
| `tests/integration/live_stack/private_images.py` (new) | staged image, quarantine put, upload with name and lifetime, version listing, purge, owner cleanup |
| `tests/integration/test_catalog_tool_cells_live.py` | import the moved helpers; behaviour unchanged |
| `tests/integration/test_operator_mutation_tool_cells_live.py` (new) | the carrier |
| `scripts/coverage_campaign/obligations.toml` | 54 `[implementations]` bindings |
| `tests/scripts/test_coverage_contract.py` | the 216 cells bind to the node; bound set grows |
| `docs/operating/runbooks/live-testing.md` | carrier section, lane prerequisites, killed-run restores |

## Task 1 — move the private-image helpers

Files: new `tests/integration/live_stack/private_images.py`; modify
`tests/integration/test_catalog_tool_cells_live.py`.

Interfaces produced:
- `staged_image() -> StagedImage` (blocked stop when none)
- `put_quarantined(source, key)`, `versions(key) -> list[str]`, `purge(key) -> bool`
- `upload_private(caller, token, db_url, *, project, name, arch, key, lifetime_s=None) -> str`

Verification:
- Contract: the catalog carrier's helpers keep their behaviour. Mode: focused-test — `just type`
  and `just lint` over both files, and
  `uv run python -m pytest tests/integration/test_catalog_tool_cells_live.py --collect-only -q`
  collects the same parameter count as on `main`.

## Task 2 — carrier frame and bindings

Files: new carrier; `obligations.toml`; `test_coverage_contract.py`.

Interfaces produced: `TOOLS`, `_functional_grants`, `_rejection`, the platform snapshot,
`test_operator_mutation_tool_cell(cell)`; rejection cells for every tool.

Verification:
- Contract: 216 cells bind to the node and the unbound set shrinks by exactly them. Mode:
  focused-test — red before the bindings land, green:
  `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`.
- Contract: collection. Mode: focused-test — `--collect-only` lists 216 parameters.

## Task 3 — resource tool bodies

Bodies for `resources.register`, `deregister`, `renew`, `set_status`, `set_scheduling`,
`ops.set_host_capacity`, `ops.set_cost_class_coeff`: runtime host `R`, placement probe, ledger
read, restores.

Verification: task-test-not-applicable offline (live stack only); `just type`, `just lint`.
Live: the 28 functional cells of these tools qualify in both configurations.

## Task 4 — queue, inventory and reconcile bodies

`ops.set_queue_paused`, `inventory.clear_override`, `ops.reconcile_systems` with the declared-host
selection, the reconcile-interval waits and the bounded race retry.

Verification: as Task 3.

## Task 5 — image bodies

`images.extend`, `images.prune_expired` (inner `on_catalog_system` run, name-matched retention).

Verification: as Task 3.

## Task 6 — authority bodies

`ops.reconcile_now` and `resources.drain` on an inner `on_authority_system` run; outer `effect`
and `cleanup` from the body plus the inner records.

Verification: as Task 3; the frame stops `blocked` without `LOCAL_AUTHORITY_LANE_CONFIG`.

## Task 7 — runbook

A section beside the other tool-cell carriers: environment, both bring-ups, the declared host
prerequisite, residue, and the restore calls after a killed run.

Verification: `just docs-check`.

## Live proof

Deploy the committed candidate to the authority lane per the root-approved private plan, then
run `default` and `recovery` bring-ups and `qualify` over the 216 cells.
