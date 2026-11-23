# Reconciler-owned teardown warning history

## Problem and scope

The actual `Reconciler.run_once` reproduction with two instances sharing configuration and a
real pool reports `[1, 0, 0]` for first/peer/first. Each owner should report `[1, 1, 0]` for the
unchanged stranded stop. Both warning families also prune process-global history from each
query's candidates. The user authorizes internal ownership changes with public APIs preserved.
[ADR-0841](../../adr/0841-teardown-warning-history-belongs-to-the-reconciler.md) records the seam.
The two exact initialization-coupling findings share this one implementation and proof.

## State and data flow

In `reconciler/repairs/systems.py`, add a slots dataclass `TeardownWarningHistory`:
`stranded: dict[UUID, datetime]` and `stalled: dict[UUID, str]`, each using `default_factory=dict`.
Remove `_warned_stranded_teardowns` and `_warned_stalled_teardowns`; no compatibility globals.
Both `report_stranded_orphan_teardowns` and `repair_stalled_tearing_down_systems` take an optional
keyword `warnings: TeardownWarningHistory | None = None`. Only `None` creates fresh history.
Use its stranded map for the existing compare/clear/update sequence and its stalled map for
candidate pruning. Pass that exact stalled map into the two existing warning helpers.
Keep all SQL values, row keys, logging text, mutation checks and transaction boundaries exact.

In `reconciler/loop.py`, add a private frozen slots `_RepairState` with two required fields:
`external_boot_cursors: external_boot_repairs.RepairCursors` and
`teardown_warnings: system_repairs.TeardownWarningHistory`. This record only carries references.
The existing fourth catalog factory argument becomes `_RepairState`; arity remains four.
Rename unused `_cursors` parameters to `_state` and adjust their annotations without changing
other factory bodies. The external-boot factory passes `state.external_boot_cursors` to its
existing helper. The two warning entries return callbacks capturing `state.teardown_warnings`.
Keep callback aliases, catalog order/membership, optional-repair gating and report mappings.

Preserve `external_boot_cursors` on `_repair_plan` and `reconcile_once`. Add optional
`teardown_warnings` alongside it. `_repair_plan` creates a fresh missing map/history and one
`_RepairState`, preserving any explicitly supplied objects even when empty. It passes that
record to each factory. `Reconciler` retains its existing cursor map and adds a fresh warning
history field, supplied by `run_once`. `ReconcileConfig` and process assembly remain unchanged.
No plan caching, object-ID registry, task-local state or new executor framework.

## Standalone calls and tests

Omitted history is intentionally one-shot, like the existing cursor convention. Repeated direct
callers that want suppression supply a single history explicitly. Migrate the orphan warning
loop test to an actual persistent `Reconciler`; migrate direct repeated repair tests to explicit
history. Remove only the fixture/imports that reset the old globals. Retain the existing
failure/cancellation/authority remedy, timestamp change and pruning assertions.

Add `tests/reconciler/test_warning_ownership.py` for real database owner isolation. Reuse existing
reconciler seeders, repair configuration and database-provisioning helpers. Any peer database
fixture owns migration and teardown in `finally`, using the existing role-creation lock.
For both warning families prove independent initial reports with a shared config/pool; an empty
peer pass against a separate database must not clear the first runner; equal System UUIDs in
separate databases must not suppress either runner. Keep unchanged row suppression, changed
cause/timestamp warnings and bounded removal/reappearance checks. Check one-shot versus explicit
standalone history and read-query failure retention. No test changes a behavioral expectation
merely to accept the refactor: ownership is explicit in every repeated-call proof.

## Failure model

- History affects warning emission and its existing metrics only; database guards own safety.
- A successful candidate query may prune its owner's current map exactly as before; another
  runner or warning family must never participate. Query failure leaves supplied maps intact.
- Restart or omitted standalone state can repeat warnings. Warning history is not persisted.
- Normal `Reconciler.run` passes are serial. Concurrent manual passes on the same owner may
  repeat observations; this change adds no new concurrency or exactly-once logging guarantee.
- External-boot cursor identity and cancellation/error checkpoint behavior remain unchanged.
- Candidate fairness and durable automatic replay budget are separate queued changes.

## Verification

Observe permanent isolation cases fail before implementation; rerun the original private
reproduction unchanged. Inject shared history, per-pass reset and cross-family pruning faults
one at a time, require named failures and restore exact source bytes. Run orphan/stalled/loop/
external-boot tests with required Docker and relevant lint/whole-tree types. Compare protected
System repair SQL, locks, admission and log strings plus loop catalog/report/other callback ASTs.
Compare all 136 tool metadata records, CLI, references and constants unchanged. Build/import
exact wheel owners, then run bounded independent scoped code/security reviews before resolution.
