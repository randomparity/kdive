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
- `tests/integration/live_stack/tool_cells.py` — `_empty_page` gains `allocations.list`;
  `test_tool_cells.py` gains its test and moves the unconfigured-page case to a synthetic tool.
- `tests/integration/live_stack/cleanup.py` — `release_and_verify` accepts an allocation
  the body already released (release cell only; see Release). This replaces the
  `authority_lane.py` frame change first scoped (scope amendment, D1).

Reused seams: `HttpCaller`, `Grants`, `prove_functional`, `prove_rejection`,
`FilteredListControl`, `project_state`, `on_lane_system`, `seed_metering`,
`capacity_in_use`, `release_and_verify`, `service_cells` grants and `_rows`.

## Fixtures and snapshot

Each cell uses a fresh `cov-<hex>` project. Admission is fail-closed without budget and
quota rows, so setup seeds them *before* the cell's snapshot: `seed_metering`, plus a
carrier-local `_seed_pending(db, project, n)` upsert of `quotas.max_pending_allocations`
on the same harness DSN where a cell queues (`seed_metering` leaves it at 0, which refuses
every queued request). Allocations are small (1 vCPU, 1 GiB, minimal disk,
`resource={"mode": "kind"}`, explicit `window`), hold capacity only, and never provision.

The accounting snapshot `_accounting_state(db, project)` is `project_state` with four
tables narrowed under ADR-0722 §4 (projections exclude `updated_at`, which the tables'
triggers move on every update):

- `allocations` → live rows only (`requested`, `granted`, `active`, `releasing`);
- `ledger` → rows of those live allocations; rows of terminal allocations are history,
  retained and never deleted;
- `budgets` → `limit_kcu`, `spent_kcu`. A never-active grant's release credits its whole
  reservation, so these cells end at the seeded `spent_kcu`;
- `quotas` → the three cap columns.

A rejected call that writes a ledger row, moves `spent_kcu` or changes a cap therefore
changes the snapshot. A cell that changes a limit or cap restores the seeded value before
it returns, so `cleanup` is the unchanged narrowed snapshot plus, for allocating cells, the
granted resource's `in_use` back at its pre-cell value. Lane-System cells (usage,
reports.generate, release) use their frame's own cleanup proof.

Independent observations read durable rows on a read-only session (`_rows`). Expected KCU
is computed in the test, never by importing the product's cost functions:
`rate = coeff × A × (vcpus + 0.25·memory_gb)` with `coeff` from `cost_class_coefficients`
for the resource's `cost_class`, and `A` from the System's or resource's accelerator
(kvm 1.0, tcg 4.0, absent 1.0); a reservation is `quantize(rate × window_hours)`; a
reconciliation is `quantize(rate × Decimal(active_seconds) / 3600 − Σreserved)`; quantize
is to 0.0001 with `ROUND_HALF_EVEN`.

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
  and writes one `reserved` row equal to rate × 1 h; an extension of 10 000 h is clamped
  (the expiry delta is below 10 000 h and the new expiry is at most the answer's
  `server_time` plus the requested span) and its `reserved` row equals rate × the actual
  expiry delta; a renew of the released allocation answers `stale_handle` and writes
  nothing. The ceiling slides with server time, so no "at the cap" refusal is asserted.
- `allocations.wait` (viewer; roles server, reconciler): quota caps 1 concurrent/1 pending;
  a holder grant and a queued request; `timeout_s=0` reports `requested` with
  `queue_position`; the holder is released through a setup token; repeated `timeout_s=60`
  waits observe `granted` within 180 s, with a resource and a `reserved` row. The test
  issues no promotion call; promotion is attributed to the deployed reconciler only through
  the recorded reconciler revision, since no durable row names the promoting actor.
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
  equal independent sums. `reports.generate` (granted-set, both formats) must return refs
  for every section CSV and the XLSX (a `spreadsheet_unavailable` answer fails the cell).
  Each downloaded spreadsheet (stdlib `csv`, `openpyxl`, already a dependency) equals the
  independent query for its section (`inventory`, `leases`, `activity`, `costs`; `images`
  against the catalog query), comparing values as strings, with `None` as empty; each
  section's `truncated` must be false for the cell's small fixture; inline `rows_json` equals
  a prefix of those rows, the whole set exactly when `inline_truncated` is false.
- `accounting.report` (viewer, then a platform_auditor token): granted-set for the project
  with `group_by=principal`, and all-projects, both over a closed past window. Rows for the
  cell's project and the totals equal independent windowed ledger sums; every project with
  window ledger rows before the call is listed.

## Release

`allocations.release` cells require server, worker, reconciler and authority revisions.
Its functional body runs on a ready local lane System (`on_lane_system`, decision D1):
release through the cell exposure as a contributor, then observe the deployed reconciler's
orphan sweep enqueue the `<system>:teardown` job, the worker run it to `succeeded`, the
System reach `torn_down`, the worker domain and disks absent, the allocation `released`,
the `reconciled` row equal to the reconciliation formula above (active seconds from
`active_started_at`/`active_ended_at`), `spent_kcu` equal to the project's ledger sum, and
the resource's capacity back at its pre-allocation value. The authority daemon takes no
part in a local release (neither a lane System nor a fixture-mode authority System is
torn down through it); the authority role is satisfied by the matching installed revision
in the cell's identity, and the release cell's effect evidence says so
(`authority_role: "installed-revision-only"`).

`release_and_verify` gains one branch keyed on the allocation alone: when
`allocations.wait(timeout_s=0)` already reports `released`, it skips its release call and
its "capacity held" precondition, then proves `torn_down`, domain/disk absence and capacity
exactly as today (the frame's `systems.teardown` before it is idempotent on `torn_down`).
A body that fails after release with the System not yet torn down takes the same branch,
which still waits for `torn_down`. Callers whose allocation is still held are unchanged.
The result carries `released_by: "body" | "frame"`; the release cell requires `"body"`, and
every caller's cleanup evidence records it.

## Rejections

All use `prove_rejection` with the narrowed snapshot as protected state.

- authentication: a forged signature answers 401; the issued token does not. That issued
  token is one role below the tool's floor, so its control call is refused by role and writes
  nothing.
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
  `{truncated: false, next_cursor: null, project: <named project>}`. A request naming no
  project fails the harness's assertion rather than a `KeyError`; the existing
  unconfigured-page test moves to a synthetic tool name.
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
  projects persist; promotion is not attributed to the reconciler by a durable row; a
  product that released an allocation early would make another frame's cleanup take the
  already-released branch and skip its "capacity held" precondition — visible only as
  `released_by: "body"` in that frame's cleanup evidence.
- Covered elsewhere: production utilization reports (#2720/#2725); authority recovery
  orphans (#3185); remote-authority teardown and POWER lanes (#3152, #2818); CLI (#3099).

## Operator decisions (approved 2026-10-20)

- D1: a local lane System with the `cleanup.py` branch, added to the scope by amendment.
  The authority-role gap of a local release (no local release reaches the authority
  daemon) is owned by #3152.
- D2: `accounting.usage` and `reports.generate` run on a lane System.
- D3: report artifacts are retained for the ADR-0212 retention sweep.

## Validation and operation gate

Focused tests inject faults into each predicate: wrong estimate/coefficient, missing or
extra ledger row, changed `spent_kcu`, a rejection that writes a ledger row, an
`updated_at`-only change (must not move the digest), wrong lease delta and clamp, wrong
page order or filter, non-canonical empty page, promotion timeout, wrong reconciled delta,
report section/CSV/XLSX mismatch or missing refs, and `release_and_verify`'s
released branch (released + torn_down, released + ready, held). Record red then green. Run `just lint`, `just type`, the coverage-contract binding
test and relevant hooks; the one managed pre-push `just ci` is the final gate.

No live run is authorized by this design. The operation packet (host, deployed revisions,
time bound, cleanup) is a separate operator grant.
