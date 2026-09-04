# Implementation plan — external-boot idempotency and bounded failures

Issue [#2202](https://github.com/randomparity/kdive/issues/2202). Spec:
[2026-09-04-external-boot-idempotency-design.md](../specs/2026-09-04-external-boot-idempotency-design.md).
Decisions: ADR-0583 and ADR-0593.

## Goal and architecture

Authority-marked external-boot jobs re-enter from durable activation and recovery-attempt state,
observe before repeating provider work, reuse absolute deadlines, and map failures into a closed
agent-readable vocabulary. The worker continues to write through the authority commit function;
the activation repository remains opaque and callers differentiate its CAS outcomes under the
System lock.

Tech stack: Python 3.14, pydantic v2, psycopg 3 async, PostgreSQL migrations, pytest and
testcontainers, managed by `uv`.

## Global constraints

- Base branch `main`; branch `feat/external-boot-idempotency-2202`.
- Host architecture `x86_64`; declared targets `x86_64` and `ppc64le`; host is included. Native
  ppc64le live testing is excluded by campaign authority.
- Guardrails: focused pytest during tasks, then `just lint`, `just type`, and pre-push
  `just ci > PRIVATE_FILE 2>&1 < /dev/null`. Never pipe a gate or append a trailing command.
- Ruff line length 100 and strict whole-tree `ty`; zero warnings.
- No new dependency, job kind, provider adapter, reconciler lane, MCP contract, or ADR.
- Migration number is preassigned: `0128`.
- Existing ADR/index coupling is hard-gated, but this change creates no ADR.
- Provider exceptions and public workflow text contain no host identifier, filesystem path, raw
  exception text, or other private identifier.

Expected implementation size: 900–1800 changed lines (L) — derived from one SQL migration, three
handler/model modules, one fault-inject module, debt closure, and focused database/handler tests.

## File map

| Path | Responsibility |
|---|---|
| `src/kdive/db/schema/0128_external_boot_reentry_failures.sql` | Idempotent mid-operation commits and bounded failure-context validation |
| `src/kdive/jobs/models.py` | Closed reason/action failure context |
| `src/kdive/jobs/handlers/external_boot/runner.py` | Observation-first execution and bounded mapping |
| `src/kdive/jobs/handlers/external_boot/lifecycle.py` | Deadline/attempt orchestration and per-operation re-entry decisions |
| `src/kdive/providers/fault_inject/lifecycle/external_boot.py` | Deterministic before/after faults, observations, and call counts |
| `tests/db/test_migration_0128_external_boot_reentry_failures.py` | SQL replay and validation contracts |
| `tests/jobs/handlers/external_boot/test_reentry.py` | Phase replay, deadlines, CAS, ledger, and failure mapping |
| `tests/providers/fault_inject/test_external_boot.py` | Fault-inject control contract |
| `tests/db/test_migrate.py` and migration-list fixtures | Migration inventory |
| `docs/debt/0005-external-boot-cas-superseded-conflation.md` | Resolution evidence |

## Task 1 — Pin the bounded failure carrier and SQL contract

**Interfaces.** `ExternalBootFailureReason` and `ExternalBootRecoveryAction` are closed literals.
`failure_context` accepts `phase`, `reason`, and `next_action`, with reason/action either both absent
or one of exactly three valid pairs. Migration 0128 preserves the commit function's signature and
grants.

**Verification**

- Mode: focused-test. Model and SQL reject an unknown reason, an unknown action, and a mismatched
  pair; red before implementation; green with
  `uv run python -m pytest tests/jobs/test_external_boot_authority_models.py tests/db/test_migration_0128_external_boot_reentry_failures.py -q`.
- Mode: focused-test. An equal replay of `deadline` or `recovery-attempt` applies without changing
  stored facts or adding an attempt; a conflicting replay is superseded; same command.

**Steps**

1. Add the model and migration tests and observe their focused red failures.
2. Extend `_FailureContext` with the closed optional pair and add migration 0128 by copying the
   current function definition, limiting edits to failure validation and idempotent mid-operation
   branches; preserve owner, grants, and signature.
3. Update migration inventories and run the focused command to green.
4. Run `just lint` and `just type`; commit as one conventional migration/model change.

## Task 2 — Add observation-first re-entry and deadline/attempt orchestration

**Interfaces.** `run_operation` accepts a pre-mutation observation decision and a constant failure
classification. Lifecycle helpers read `activation_readiness_deadline` and the current
`ExternalBootRecoveryAttempt`, emit the existing `deadline`/`recovery-attempt` result variants, and
consume every `CasStatus`. Public handler signatures stay unchanged.

**Verification**

- Mode: focused-test. Matching provider observation skips activate/recover; non-matching observation
  invokes mutation once and observes again; worker loss replay preserves durable rows apart from
  `updated_at`; red first and green with
  `uv run python -m pytest tests/jobs/handlers/external_boot/test_reentry.py -q`.
- Mode: focused-test. Existing activation and recovery deadlines are reused, expiry reaches
  recovery/recovery-failed, and conflict resolution creates a new attempt/deadline from its own
  server time; same command.
- Mode: focused-test. NOT_FOUND, missing ready reservation, and lost generation yield the three
  fixed reason/action pairs; no `IllegalTransition` escapes; same command.

**Steps**

1. Add re-entry tests for the six issue-named boundaries using the existing seeded Postgres vehicle;
   observe mutation-count, row-equality, observation-order, deadline, and ledger failures.
2. Add small runner helpers that build constant-context failures, decide observation-first skips,
   and translate repository outcomes without exposing predicate details.
3. Add lifecycle deadline and attempt orchestration using existing result variants and repository
   reads. Reuse persisted values; create a deterministic attempt id only on the first edge.
4. Terminalize expired recovery attempts through `finish_recovery_attempt`; retain evidence.
5. Run the focused test, `just lint`, and `just type`; commit the behavior and tests.

## Task 3 — Make fault injection enumerate the failure map

**Interfaces.** `FaultInjectExternalBoot` exposes deterministic configuration at construction,
operation call counts, ordered call history, and before/after-mutation fault points. Its ordinary
zero-argument construction remains compatible.

**Verification**

- Mode: focused-test. Every configured fault point raises once at its named boundary and exposes
  call count/order; red first and green with
  `uv run python -m pytest tests/providers/fault_inject/test_external_boot.py tests/jobs/handlers/external_boot/test_reentry.py -q`.
- Mode: focused-test. The handler mapping is total over the explicit injected fault tuple, pins the
  result categories, and serialized results omit raw messages, paths, and host text; same command.

**Steps**

1. Write provider and handler enumeration tests and observe red.
2. Add the smallest in-memory fault controller to the existing provider; do not add a framework or
   dependency.
3. Pin the category tuple and failure serialization, then run the focused command to green.
4. Run `just lint` and `just type`; commit.

## Task 4 — Close the owned debt and verify the branch

**Interfaces.** Debt record 0005 changes `Open` to `Resolved` and names the exact migration,
handlers, and tests proving each reason/action path. No other debt record changes.

**Verification**

- Mode: task-test-not-applicable. The debt status is human-readable provenance and has no executable
  consumer beyond repository document guards; its cited executable contracts are verified in Tasks
  1–3.

**Steps**

1. Update debt 0005 with resolution evidence and verify no unresolved promise remains.
2. Run `just test-changed`, `just lint`, and `just type` bare.
3. Run `just ci > PRIVATE_FILE 2>&1 < /dev/null`; inspect the private output on failure without
   replacing the recipe's exit status.
4. Commit the debt resolution separately after green focused gates.

## Durable handoff

BASE_BRANCH is `main`; branch is `feat/external-boot-idempotency-2202`; review depth is iterating;
claim/scope token is `q2202-ce529ae2`. Open findings and review deferrals are recorded here after
the design and branch review phases.
