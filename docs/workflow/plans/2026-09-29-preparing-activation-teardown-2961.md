# Teardown of a preparing external-boot activation (#2961) — plan

Spec: [2026-09-29-preparing-activation-teardown-2961-design.md](../specs/2026-09-29-preparing-activation-teardown-2961-design.md).

## Global Constraints

- No migration, no ADR, no new public field. ADR 0620 and migrations 0147/0161 already admit a
  `preparing` activation for authority teardown.
- Every purpose other than `teardown` keeps the current `preparing` admission and preparation
  behavior.
- Guardrails: `just lint`, `just type`, `just test-changed`; pre-push `just ci`.

## File map

| File | Change |
|------|--------|
| `src/kdive/jobs/handlers/external_boot/admission.py` | plan checks skip `purpose == "teardown"` |
| `src/kdive/jobs/handlers/external_boot/runner.py` | `_prepares` predicate gates debit, materialize, and the preparation-executor refusal |
| `tests/jobs/handlers/external_boot/support.py` | `RecordingTeardownExecutor` proves `complete_pending` for a pending reservation |
| `tests/jobs/handlers/external_boot/test_admission.py` | activate still refused without a plan; teardown admitted |
| `tests/jobs/handlers/external_boot/test_prepared_before_admission.py` | handler teardown of a preparing activation |
| `tests/mcp/lifecycle/test_systems_tools.py` | `systems.teardown` on a preparing activation enqueues the marker |

## Task 1 — admission admits teardown of a preparing activation

1. Add `test_only_teardown_admits_a_preparing_activation_without_its_plan`, parametrized over
   `activate` and `teardown`, seeding a `preparing` activation with no materialization or
   recovery point. `activate` raises "requires its durable preparation plan"; `teardown` returns
   `JobKind.TEARDOWN` and a `TeardownPayload`.
2. Run it: the `teardown` case fails with the plan refusal.
3. In `build_external_boot_payload`, change the guard to
   `activation.state.value == "preparing" and purpose != "teardown"`.
4. Run it: both cases pass.

Verification: `uv run python -m pytest -q tests/jobs/handlers/external_boot/test_admission.py`.

## Task 2 — the runner does not prepare for teardown

1. Extend `RecordingTeardownExecutor._proof` to return a `complete_pending` proof with
   `pending_system_teardown` cleanup evidence when the reservation is `pending`.
2. Add `test_teardown_of_a_preparing_activation_skips_preparation`: seed `purpose="teardown"`,
   `activation_state="preparing"` (the seeder adds the pending reservation), dispatch the
   teardown handler with no preparation executor. Assert one authority teardown call, and the
   activation `torn_down`, `materialization` NULL, cleanup mode `pending_system_teardown`,
   System `torn_down`, no reservation row, no release row.
3. Run it: it fails with "no external-boot authority preparation executor is configured".
4. Add `_prepares(activation, marker)` to `runner.py` and use it in `_debit_preparing`,
   `_materialize_preparing`, and the preparation-executor refusal.
5. Run it and the rest of the file: all pass.

Verification:
`uv run python -m pytest -q tests/jobs/handlers/external_boot/test_prepared_before_admission.py`.

## Task 3 — MCP proof

1. Add `test_teardown_of_preparing_activation_enqueues_authority_marker`: a READY System, a
   RUNNING Run, a `preparing` activation with a pending reservation, and a `current` activate
   authority. `systems.teardown` as ADMIN returns `queued`; the job is `teardown` with the
   teardown marker, no `external_boot_plan_v1`, and the reservation is still `pending`.
2. Confirm it fails without the Task 1 change and passes with it.

Verification: `uv run python -m pytest -q tests/mcp/lifecycle/test_systems_tools.py -k preparing`.

## Rollback

Revert the two source hunks. The tests then fail, which is the intended signal; no data or
schema change needs undoing.
