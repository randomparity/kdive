# Implement the per-call teardown executor (#2878)

Issue: #2878. Base: `main`. Branch: `feat/teardown-executor-2878`.
Spec: `docs/workflow/specs/2026-09-28-teardown-per-call-executor-design.md`.
Goal: authority teardown on production-assembled workers uses the per-call authority client.
Architecture: `runner.run_operation` already resolves per-call ports; `OperationContext` gains the
resolved teardown executor and `teardown_handler`'s callback reads it from the context.
Tech stack: Python 3.14, psycopg, pytest against disposable Postgres (Docker).
Fixed design denominator: 250 changed lines (M), from issue effort:M.
Expected implementation size: 110–150 changed lines (M) — two source lines plus one field, two
focused tests and their seeding helpers.

## Global Constraints

- ADR-0620: teardown is authority-owned; never fall back to ordinary worker teardown.
- No new client, mutation path, schema, migration, dependency, or public contract.
- Guardrails: `just lint`, `just type` (whole tree), focused `just test-verbose <paths>`,
  `just records`; the pre-push hook runs `just ci`.

## File map

- `src/kdive/jobs/handlers/external_boot/runner.py` — owns per-call port resolution and
  `OperationContext`; gains `teardown_executor` on the context, set at its one construction site.
- `src/kdive/jobs/handlers/external_boot/lifecycle.py` — `teardown_handler.complete()` reads
  `context.teardown_executor` instead of the captured `ports.teardown_executor`.
- `tests/jobs/handlers/external_boot/test_lifecycle.py` — two focused tests and helpers.

No caller migration: `complete()` is the only reader of `teardown_executor`; no obsolete path.

## Task 1: Teardown uses the per-call executor

Verification:

- Contract: factory-only ports complete teardown once, credit the ready reservation once, and a
  retry of the finished job credits nothing. Mode: focused-test.
  Test: `test_factory_only_teardown_uses_the_per_call_executor`. Red before the fix:
  `ExternalBootAuthorityFailure` logged with `reason=no external-boot authority teardown executor
  is configured`. Green: `uv run python -m pytest tests/jobs/handlers/external_boot/test_lifecycle.py
  -q -k factory_only_teardown` reports `1 passed`.
- Contract: no factory and no executor fails closed with terminal `configuration_error`, no
  receipt, the reservation still ready, the System still `failed`. Mode: focused-test.
  Test: `test_teardown_without_factory_or_executor_fails_closed`; it guards the refusal the fix
  keeps, so it passes before and after; the controlled fault is deleting the `None` refusal in
  `complete()` (red: `AttributeError` classified as `infrastructure_failure`, not
  `configuration_error`). Green: same command with `-k without_factory_or_executor`, `1 passed`.

Steps:

1. Add the tests. `_drive_ready_reservation_teardown` seeds `seed_case(..., purpose="teardown",
   operation="teardown", activation_state="activating", with_reservation=True)` and asserts
   `_teardown_credit_counts` is `(1, 0, 0)` (ready reservations, release credits, receipts). The
   factory test builds `ExternalBootHandlerPorts(resolver=resolver_for(case.vehicle),
   incarnation_credential=..., secret_registry=SecretRegistry(), artifact_store=INERT_OBJECT_STORE,
   authority_client_factory=lambda _b, _m, _d: Client())` where `Client` delegates `acknowledge` to
   `RecordingAcknowledger` and `execute_teardown` to the file's `_TeardownExecutor`, recording
   requests. It runs the handler, then re-runs it expecting `CategorizedError`, and asserts one
   request, System `torn_down`, job `succeeded`, counts `(0, 1, 1)`. The fail-closed test uses the
   file's `_ports(...)` (no teardown executor, no factory) and asserts the failure fields and
   counts `(1, 0, 0)`.
2. Run the two tests; confirm the factory test is red with the message above.
3. In `runner.py`, add to `OperationContext` after `prerequisites`:
   `teardown_executor: ExternalBootAuthorityTeardownExecutor | None = None` (import the protocol
   from `.ports`), and pass `teardown_executor=ports.teardown_executor` in the
   `OperationContext(...)` call inside `run_operation`.
4. In `lifecycle.py` `teardown_handler.complete()`, replace `executor = ports.teardown_executor`
   with `executor = context.teardown_executor`.
5. Run both tests green, then `just test-verbose tests/jobs/handlers/external_boot
   tests/integration/test_external_boot_job_lifecycle.py`, `just lint`, `just type`; commit.

## Task 2: Live retained-fixture proof

Verification: Mode: task-test-not-applicable — the retained fixture exists only on the operator
host; no repository test can hold its state. Evidence is recorded publicly without host
identifiers in the PR.

Steps: deploy this branch to the Ubuntu 26.04 host through its supported install path; confirm
the deployed tree contains `teardown_executor=ports.teardown_executor` in `runner.py`; start the
stack and worker through the supported lifecycle; let the worker reclaim the stale teardown job
(or re-issue `systems.teardown` if the job already terminalized); verify job and System terminal
state, domain absent from libvirt, activation cleanup evidence, one release credit and no ready
reservation; re-issue `systems.teardown` and confirm refusal with no second credit. If the stale
job does not recover through the supported path, stop and report it as the excluded follow-up.
