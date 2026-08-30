# External-boot admission and agent contracts implementation plan

## Goal and architecture

Implement issue #2117 through one System-locked admission matrix and three truthful agent contracts.
Release and conflict resolution persist an idempotent ADR-0584 BOOT-job request; #2118 executes it.

Tech stack: Python 3.14, psycopg 3, PostgreSQL, FastMCP, Pydantic, pytest, uv, and just.

## Global constraints

- Support x86_64 and ppc64le and add no dependency.
- Follow accepted ADR-0583 and ADR-0584; no new ADR or migration number is assigned.
- Do not implement job handlers, reconciliation, or provider mechanisms.
- Never report orphan repair success while its executor is unavailable.
- Guardrails: `just lint`, `just type`, focused `just test-verbose <path>`, and `just ci`.
- Branch: `feat/external-boot-admission-agent-contracts-2117`; base: `main`.

## Task 1: Centralize the System-wide matrix

Files: add `src/kdive/services/external_boot/{__init__,admission}.py`; modify
`src/kdive/db/external_boot_activations.py`; add `tests/services/external_boot/test_admission.py`.

Interfaces: `ExternalBootOperation` is a closed `StrEnum` and
`check_external_boot_admission(conn, system_id, operation, *, run_id=None)` returns the live
activation/authority binding or raises `CategorizedError(CONFLICT)`. Repository method
`get_restricting_for_system(conn, system_id)` returns the one non-clean activation or `None`.

1. Write table tests for all states, cleaned terminals, active owning/foreign Run operations, and
   denial details/actions. Run the focused test and expect missing imports.
2. Add the repository lookup and pure matrix service. Re-run and expect pass.
3. Add PostgreSQL tests proving the query sees cleanup-pending terminal rows and ignores cleaned
   rows. Run `just test-verbose tests/services/external_boot/test_admission.py` and expect pass.

Acceptance: one closed table decides every operation and denial output uses the stable taxonomy.

## Task 2: Apply reverse admission

Files: modify Run admission/install, System admin/snapshot, control capture/power/crash, vmcore, and
DebugSession lifecycle call sites; extend their matching tests.

Interfaces: every mutating caller holds `LockScope.SYSTEM` and calls Task 1 immediately before its
durable enqueue/transition. Teardown passes `TEARDOWN`; active capture/crash/debug passes its Run id.

1. Add focused negative tests at each family plus a PostgreSQL barrier race between foreign-Run
   install and release admission. Run them and expect operations currently pass.
2. Insert the minimum guards and lock-boundary changes. Do not change read-only tools or provider
   code. Re-run the named tests and expect pass.

Acceptance: no reverse operation can cross a newly committed restriction; exactly one race side wins.

## Task 3: Persist recovery requests

Files: add `src/kdive/services/external_boot/recovery_requests.py`; minimally extend job payload
models needed for the versioned ADR-0584 marker; add focused service tests.

Interfaces: `request_release(pool, ctx, run_id, idempotency_key, timeout_seconds)` and
`resolve_conflict(pool, ctx, system_id, operation, observed_identity, idempotency_key,
timeout_seconds)` return `ToolResponse`. Both enqueue `JobKind.BOOT` with the existing
`ExternalBootAuthorityMarkerV1` shape, a stable operation identity, attempt id, database server time,
and one absolute recovery deadline. Repeat returns the same job/deadline.

1. Test RBAC, state ownership, session/job exclusion, exact conflict digest, rollback, replay, and
   deadlines; confirm failures.
2. Add the minimum payload/enqueue and service transaction. Do not add a handler. Re-run focused
   tests and expect pass.

Acceptance: queued rows satisfy migration 0122's allocation marker contract; no tool claims worker
execution exists.

## Task 4: Expose MCP contracts

Files: add wrappers under lifecycle runs/systems and ops, update their registrars and agent-facing
tool metadata, and add MCP schema/envelope tests.

Interfaces: `runs.release_external_boot`, `systems.resolve_external_boot_conflict`, and
`ops.resolve_recovery_orphan`. The first two return running job envelopes; repair returns only the
truthful unavailable failure until its executor exists.

1. Write schema/RBAC/envelope tests including every deadline field and literal next action; confirm
   failure before registration.
2. Add wrappers, `Field` descriptions, docstrings, registration, and exposure metadata. Re-run the
   focused tests and expect pass.

Acceptance: FastMCP schemas contain the complete unit, clock, scope, consequence, and recovery
contract, and all responses are valid `ToolResponse` values.

## Task 5: Integrate and verify

1. Run all focused changed tests, `just lint`, and `just type`; expect exit 0.
2. Run adversarial and security reviews; fix defensible in-scope findings and record any deferral.
3. Simplify without changing behavior, then run `just ci`; expect exit 0.
4. Deliver a PR closing #2117, publish `WORK:REVIEW`, and hand off the exact merge-ready SHA without
   merging.

Rollback is `git revert` of the branch. Queued external requests remain durable and must be drained
or executed by #2118 before disabling the corresponding worker contract.
