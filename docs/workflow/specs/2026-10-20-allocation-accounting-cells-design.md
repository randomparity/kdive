# Allocation, accounting and report service cells (#3098)

## Authority and scope

Frozen scope: issue #3098's `WORK:SCOPE`, token `q3098-81d084a0`, campaign
`2d21598e2016-b01982d7-4e2e-4af3-82b2-4b8aec96dc18`; scope/exclusion packet SHA256
`5ce7042937eaa47a8e1cfb10ba07265e720dac9be3d037e655b927ac17b29b50` (operator, 2026-10-16).

Implement the eleven owned tools' 55 scenario bindings and their 220 generated cells
(44 functional, 176 rejection; default/recovery × direct/gateway), including the 8
`allocations.list` filtered-list cells (ADR-0756 as amended 2026-10-13). No cell is optional.

Exclusions: run/system/job carrier (#3097, merged), production utilization reports
(#2720/#2725), CLI (#3099), authority orphan/conflict construction (#3185), native POWER
(#2818). No production, schema, migration, role/grant or provider change. No ADR: every
decision below applies an accepted record (ADR-0722 §4 narrowed snapshots, ADR-0756,
ADR-0212 report retention, ADR-0715 evidence). Reserved ADR 0790 is unused.

## Components

- `tests/integration/live_stack/allocation_accounting_cells.py` — the carrier: `TOOLS`,
  `_scenario`, per-tool functional bodies, rejection arguments, the accounting snapshot.
- `tests/integration/test_allocation_accounting_tool_cells_live.py` — the live node,
  parametrized over `tool_cells(TOOLS)` and run through `run_tool_cell`.
- `tests/integration/live_stack/test_allocation_accounting_cells.py` — focused tests that
  inject faults into the carrier's assertion predicates.
- `scripts/coverage_campaign/obligations.toml` — 55 `tool/<name>/default/<scenario>` bindings.
- `tests/integration/live_stack/tool_cells.py` — `_empty_page` gains `allocations.list`.
- `tests/integration/live_stack/authority_lane.py` — `_cleanup` accepts an allocation the
  body already released (release cell only; see Release).

Reused seams: `HttpCaller`, `Grants`, `prove_functional`, `prove_rejection`,
`FilteredListControl`, `project_state`, `on_lane_system`, `on_authority_system`,
`seed_metering`, `capacity_in_use`, `service_cells` grants and `_rows`.

## Fixtures and snapshot

Each cell uses a fresh `cov-<hex>` project. Admission is fail-closed without budget and
quota rows, so setup seeds them (`seed_metering`, plus `max_pending_allocations` where a
cell queues) *before* the cell's snapshot. Allocations are small (1 vCPU, 1 GiB, minimal
disk, `resource={"mode": "kind"}`), hold capacity only, and never provision.

The accounting snapshot `_accounting_state(db, project)` is `project_state` with four
tables narrowed under ADR-0722 §4:

- `allocations` → live rows only (`requested`, `granted`, `active`, `releasing`);
- `ledger` and terminal allocations are history: retained, never deleted, excluded;
- `budgets` → `limit_kcu` only (`spent_kcu` is the ledger's running total, history);
- `quotas` → all caps.

A cell that changes a limit or cap restores the seeded value before it returns, so
`cleanup` is the unchanged narrowed snapshot plus, for allocating cells, the granted
resource's `in_use` back at its pre-cell value.

Independent observations read durable rows on a read-only session (`_rows`); expected
KCU values are computed in the test from `cost_class_coefficients.coeff` and the documented
formula `coeff × A(accel) × (vcpus + 0.25·memory_gb) × hours` (A: kvm 1.0, tcg 4.0),
quantized to 0.0001, never by importing the product's cost functions.

## Functional observations

Each functional call uses the cell's exposure and the tool's minimum role; setup calls
use a separate admin token and do not prove the tool.

- `accounting.estimate` (viewer): two windows and both accelerators; each answer equals the
  independent formula; the call writes nothing.
- `allocations.request` (contributor): a granted allocation whose row, `reserved` ledger
  row and `spent_kcu` increment equal the independent estimate; a second request with
  `on_capacity=queue` at the quota cap is `requested` with no resource, lease or ledger row.
  Cleanup releases both; the never-active grant's `reconciled` credit returns `spent_kcu`.
- `allocations.list` (viewer): granted, queued and released allocations; unfiltered and
  per-state pages equal the independent `created_at DESC, id DESC` order; paging with
  `limit=1` drains the same set through `next_cursor`.
- `allocations.renew` (contributor): `extend=1` moves `lease_expiry` by exactly one hour
  and writes one `reserved` row equal to rate × 1 h; an extension past `KDIVE_LEASE_MAX`
  clamps (charge equals rate × the actual expiry delta); a renew at the cap answers
  `configuration_error` and writes nothing; a renew of the released allocation answers
  `stale_handle`.
- `allocations.wait` (viewer; roles server, reconciler): quota caps 1 concurrent/1 pending;
  a holder grant and a queued request; `timeout_s=0` reports `requested` with
  `queue_position`; the holder is released through a setup token; repeated `timeout_s=60`
  waits observe `granted` within 180 s, promoted by the deployed reconciler's own sweep
  (no `ops.reconcile_now`), with a resource and a `reserved` row.
- `allocations.release`: see Release.
- `accounting.set_budget` (admin): after one grant, the limit changes and `spent_kcu` does
  not; with limit `spent + E − 0.0001` a request of estimate `E` is denied
  `allocation_denied` with a budget `unmet` entry; at `spent + E` it is granted.
- `accounting.set_quota` (admin): every cap reads back; at caps (1, 0, 1) the second
  concurrent request is `quota_exceeded`, a first queued request is accepted and a second
  is `quota_exceeded`, and `systems.provision` on the grant is refused `quota_exceeded`
  with no System row or job.
- `accounting.usage` and `reports.generate` (viewer) run on `on_lane_system`, because
  investigation attribution needs a Run on the allocation's System (ledger.py). A Run bound
  to the System in one investigation makes `investigation_kcu` equal that allocation's
  ledger sum; project `spent_kcu`, `budget_remaining`, `shared_kcu` and `by_cost_class`
  equal independent sums. `reports.generate` (granted-set, both formats) is compared section
  by section (`inventory`, `leases`, `activity`, `costs`; `images` against the catalog
  query) with the inline `rows_json`, and each presigned CSV and the XLSX is downloaded and
  parsed (stdlib `csv`, `openpyxl`, already a dependency) and must equal the inline rows.
- `accounting.report` (viewer, then a platform_auditor token): granted-set for the project
  with `group_by=principal`, and all-projects, both over a closed past window. Rows for the
  cell's project and the totals equal independent windowed ledger sums; every project with
  window ledger rows before the call is listed.

## Release

`allocations.release` requires server, worker, reconciler and authority. Its functional
body runs on the local fixture-mode authority System (`on_authority_system`): drain the
System's jobs, release through the cell exposure as a contributor, then observe the
deployed reconciler's orphan sweep enqueue the `<system>:teardown` job, the worker run it
to success, the System reach `torn_down`, the domain and storage absent from the authority
inventory, the allocation `released`, the `reconciled` row equal to
rate × active hours (from `active_started_at`/`active_ended_at`) minus Σreserved, and the
resource's capacity reclaimed. `_cleanup` then finds the allocation released: it still
drains, closes investigations, removes the authority fixture and proves domain, disk,
capacity and inventory absence, but skips the teardown/release calls and the "capacity
held" precondition. Every other frame user is unchanged.

## Rejections

All use `prove_rejection` with the narrowed snapshot as protected state.

- authentication: a forged signature answers 401; the issued token does not.
- validation: missing required arguments (`{}`), or `{"request": {"limit": "many"}}` for
  `allocations.list`.
- authorization: a member without a role for viewer-floor tools, a viewer for
  contributor tools, a contributor for admin tools → `authorization_denied`. Allocation-id
  tools target a real granted allocation of the project.
- project-isolation: a stranger naming the project → `authorization_denied`; for
  `allocations.wait/renew/release` → `not_found` with an absent-id twin whose masked
  answer must be identical.
- `allocations.list` authorization/project-isolation: `FilteredListControl` (a viewer lists
  the real allocation), then the denied caller must receive exactly the canonical page:
  object `allocations`, no items, no next actions, data
  `{truncated: false, next_cursor: null, project: <named project>}`.
- `accounting.report`/`reports.generate` use granted-set with `projects=[<project>]`,
  because an unnamed granted-set cannot be denied.

## Cleanup and retention

Allocations are released in `finally`; cleanup failure joins the primary error.
`reports.generate` artifacts (owner `reports`) are retained under ADR-0212's reconciler
retention sweep: the cell records their artifact ids, keys and `created_at` and verifies
the owner kind, but does not prove post-retention collection. Budget/quota rows and
ledger history of throwaway projects are retained.

## Failure model

- Actors and deployments: the campaign operator runs the node on one exclusive local
  x86_64 live stack (demo-up, candidate-matched server/worker/reconciler, installed local
  authority lane) in default then recovery configuration, cells serially.
- Invariants and assets: honest outcomes (a missing role or prerequisite is blocked, a
  product mismatch fails); no live allocation, System, domain or storage left behind;
  exact deployed identity per cell; other projects' state untouched.
- Accepted: concurrent foreign traffic on the stack would perturb capacity and
  all-projects totals — the lane is exclusive by the operation plan; report artifacts
  live until the retention sweep; budget/quota rows and ledger history of throwaway
  projects persist; the authority teardown race with the fixture is observed, not
  prevented — a failure is recorded as failing.
- Covered elsewhere: production utilization reports (#2720/#2725); authority recovery
  orphans (#3185); remote and POWER lanes (#2818); CLI (#3099).

## Validation and operation gate

Focused tests inject faults into each predicate: wrong estimate/coefficient, missing or
extra ledger row, changed `spent_kcu`, wrong lease delta and clamp, wrong page order or
filter, non-canonical empty page, promotion by a non-reconciler path or timeout, wrong
reconciled delta, report section/CSV/XLSX mismatch, and the frame's released-allocation
branch. Record red then green. Run `just lint`, `just type`, the coverage-contract binding
test and relevant hooks; the one managed pre-push `just ci` is the final gate.

No live run is authorized by this design. The operation packet (host, deployed revisions,
time bound, cleanup) is a separate operator grant.
