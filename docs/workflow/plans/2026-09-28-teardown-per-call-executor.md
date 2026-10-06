# Implement the per-call teardown executor (#2878)

Issue: #2878. Base: `main`. Branch: `feat/teardown-executor-2878`.
Spec: `docs/workflow/specs/2026-09-28-teardown-per-call-executor-design.md`.
Goal: authority teardown on production-assembled workers uses the per-call authority client.
Architecture: `runner.run_operation` already resolves per-call ports; `OperationContext` gains the
resolved teardown executor and `teardown_handler`'s callback reads it from the context.
Tech stack: Python 3.14, psycopg, pytest against disposable Postgres (Docker).
Fixed design denominator: 250 changed lines (M), from issue effort:M.
Expected implementation size: 110–150 changed lines (M) — one context field, one runner refusal,
one callback line, two focused tests and their seeding helpers.

## Global Constraints

- ADR-0620: teardown is authority-owned; never fall back to ordinary worker teardown.
- No new client, mutation path, schema, migration, dependency, or public contract.
- Guardrails: `just lint`, `just type` (whole tree), focused `just test-verbose <paths>`,
  `just records`; the pre-push hook runs `just ci`.

## File map

- `src/kdive/jobs/handlers/external_boot/runner.py` — owns per-call port resolution and
  `OperationContext`; gains `teardown_executor` on the context, set at its one construction site,
  and refuses a teardown with no per-call executor before authority allocation.
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
- Contract: no factory and no executor refuses with terminal `configuration_error` before
  authority allocation: zero authority rows, no receipt, reservation still ready, System still
  `failed`. Mode: focused-test. Test: `test_teardown_without_factory_or_executor_fails_closed`.
  Red before the fix: the refusal is a bound `ExternalBootAuthorityFailure` after allocation, so
  the authority-row count is 1. Green: same command with `-k without_factory_or_executor`,
  `1 passed`.

Steps:

1. Add the tests. `_drive_ready_reservation_teardown` seeds `seed_case(..., purpose="teardown",
   operation="teardown", activation_state="activating", with_reservation=True)` and asserts
   `_teardown_credit_counts` is `(1, 0, 0)` (ready reservations, release credits, receipts). The
   factory test builds `ExternalBootHandlerPorts(resolver=resolver_for(case.vehicle),
   incarnation_credential=..., secret_registry=SecretRegistry(), artifact_store=INERT_OBJECT_STORE,
   authority_client_factory=lambda _b, _m, _d: Client())` where `Client` delegates `acknowledge` to
   `RecordingAcknowledger` and `execute_teardown` to the file's `_TeardownExecutor`, recording
   requests. It runs the handler, then re-runs it expecting `CategorizedError` with category
   `configuration_error`, and asserts one request, one `external_boot_authorities` row for the
   activation, System `torn_down`, job `succeeded`, counts `(0, 1, 1)`. The fail-closed test uses
   the file's `_ports(...)` (no teardown executor, no factory), expects `CategorizedError` with
   `configuration_error` and `terminal`, and asserts zero authority rows, System `failed`, counts
   `(1, 0, 0)`.
2. Run the two tests; confirm the factory test is red with the message above.
3. In `runner.py`, add to `OperationContext` after `prerequisites`:
   `teardown_executor: ExternalBootAuthorityTeardownExecutor | None = None` (import the protocol
   from `.ports`), and pass `teardown_executor=ports.teardown_executor` in the
   `OperationContext(...)` call inside `run_operation`.
4. In `run_operation`, immediately before `allocate_authority`, add
   `if marker.operation == "teardown" and ports.teardown_executor is None: raise _refuse("no
   external-boot authority teardown executor is configured")`, after the per-call `replace`.
5. In `lifecycle.py` `teardown_handler.complete()`, replace `executor = ports.teardown_executor`
   with `executor = context.teardown_executor`.
6. Run both tests green, then `just test-verbose tests/jobs/handlers/external_boot
   tests/integration/test_external_boot_job_lifecycle.py`, `just lint`, `just type`; commit.

## Task 2: Live retained-fixture proof

Verification: Mode: task-test-not-applicable — the retained fixture exists only on the operator
host; no repository test can hold its state. Evidence is recorded publicly without host
identifiers in the PR.

Steps: deploy this branch to the Ubuntu 26.04 host through its supported install path; confirm
the deployed tree contains `teardown_executor=ports.teardown_executor` in `runner.py`; start the
stack and worker through the supported lifecycle; let the worker reclaim the stale teardown job
through the supported path; verify job and System terminal state, domain absent from libvirt,
activation cleanup evidence, one teardown receipt, one release credit and no ready reservation;
re-issue `systems.teardown` and confirm it replays the same job envelope with credit still one
release row. If the stale job does not recover through the reclaim path, stop and report it as the
excluded follow-up; do not re-issue teardown as a substitute.
