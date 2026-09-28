# Per-call authority executor for System teardown (#2878)

## Scope and authority

Campaign scope for issue #2878, token `q2878-aaa08220`. The operator approved these exclusions
on 2026-09-28: a separately reproduced stale-job reclaim or failure-commit defect (new finding
issue through the orchestrator); the host accelerator mismatch (#2877); manual database or
journal repair of the retained fixture (operator, not authorized); post-teardown mutation
obligations (closed #2326); release/activate captured-ports consistency; and the fresh installed
normal-operations carrier run, which #2877's live run verifies after both merge.

ADR-0620 governs: teardown is authority-owned, journal-backed, and never falls back to ordinary
worker teardown. This change restores that path on production-assembled workers; it records no
new decision.

## Problem

`register_all_handlers` builds `ExternalBootHandlerPorts` with `authority_client_factory` and no
direct `teardown_executor`. `runner.run_operation` creates the per-call client and rebinds
its local `ports` with `teardown_executor=client`, but `teardown_handler`'s `complete()` callback
closes over the builder's `ports` and reads `ports.teardown_executor`, which is `None`. Every
admitted teardown on a production worker therefore refuses with `no external-boot authority
teardown executor is configured` after authority allocation and acknowledgement, retaining the
Allocation and the ready reservation. Tests inject `teardown_executor` directly, so none observe
the closure.

## Design

`OperationContext` already carries the per-call `authority_executor`. Add a sibling field
`teardown_executor: ExternalBootAuthorityTeardownExecutor | None = None`, populated from the
per-call `ports.teardown_executor` at the one construction site in `runner.py`. `complete()` reads
`context.teardown_executor` and keeps its existing `None` refusal. The per-call value is the
factory client when a factory is configured, and the directly injected executor otherwise, so
existing test wiring is unchanged. No new client, mutation path, caller migration, schema, or
public contract. `artifact_store` and `incarnation_credential` still come from the captured ports;
the per-call `replace` never changes them.

The runner also refuses a teardown marker whose per-call `teardown_executor` is `None` before
authority allocation, beside the existing missing-`preparation_executor` refusal. On `main` that
refusal fired inside `complete()`, after allocation and acknowledgement, consuming a generation
and journaling an acknowledgement for a teardown that could never run. `complete()` keeps its own
`None` refusal for type narrowing.

Rejected alternatives: putting the executor in `prerequisites` like release does (an untyped
`Mapping[str, Any]` lookup where a typed field already exists beside it); re-reading
`authority_client_factory` inside `complete()` (a second client per call, which the issue forbids).

## Success

1. With factory-only ports (no direct `teardown_executor`), an admitted teardown calls the
   factory client's `execute_teardown` exactly once, finalizes the receipt as `applied`, leaves the
   System `torn_down`, the job `succeeded`, and exactly one reservation release row.
2. Re-invoking the handler for the same job after that success is refused at admission with
   `configuration_error`, allocates no second authority row, and does not call `execute_teardown`
   again. Exactly-once credit itself rests on the release table's `activation_id` primary key and
   the receipt's `root_authority_id` primary key with `applied` replay in the SQL finalizer.
3. With neither a factory nor a direct executor, teardown refuses with terminal
   `configuration_error` before authority allocation: no authority row, acknowledgement, receipt,
   or release row is written, and the System stays `failed` with its reservation ready.
4. Existing teardown tests (direct executor, quarantine, transport failure, public
   `systems.teardown` claim) stay green unchanged.
5. Live: on the retained Ubuntu 26.04 fixture, with this branch deployed, the supported worker
   reclaim of the stale teardown job completes authority teardown: terminal job and System, domain
   absent, cleanup evidence recorded, one teardown receipt, and the ready reservation credited
   once. A repeated `systems.teardown` replays the same job envelope (dedup key, no recycle), so
   it proves only that credit stays at one release row. If the stale job does not recover through
   the reclaim path, the proof stops and reports the excluded follow-up. No host reset or manual
   database/journal edit.

## Failure model

- Actors and deployments: the jobs worker on operator hosts (production assembly with the
  authority client factory); tests with directly injected executors.
- Invariants and assets: exactly-once reservation credit (release row) and teardown receipt;
  authority fencing (incarnation, job attempt, lease, journal head) in
  `finalize_external_boot_authority_teardown`; destructive domain removal only through the
  authority.
- Accepted failure classes: a lost authority response or client-deadline expiry after the
  authority applied teardown, and core reclaim before a `superseded` finalize. Both are existing
  behaviour this fix first exposes in production, not changed or proven here; retry convergence
  rests on the authority's `attempt_id` replay and the primary keys above. The per-call client
  deadline for teardown is `recovery_readiness_timeout` (default five minutes), so a live timeout
  is reported as that, not as this defect.
- Covered elsewhere: stale-job reclaim/failure-commit behaviour if it reproduces with a working
  executor (follow-up via the orchestrator); release/activate captured-ports use (out of scope);
  host accelerator mismatch (#2877).

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| Criterion 1 | focused-test | `test_factory_only_teardown_uses_the_per_call_executor` in `tests/jobs/handlers/external_boot/test_lifecycle.py`; red before the fix with the missing-executor configuration error |
| Criterion 2 | focused-test | same test re-invokes the handler and asserts `configuration_error`, one authority row, one executor call, one release row and receipt |
| Criterion 3 | focused-test | `test_teardown_without_factory_or_executor_fails_closed` in the same file; asserts zero authority rows; red on `main` (post-allocation refusal) |
| Criterion 4 | focused-test | `just test-verbose tests/jobs/handlers/external_boot tests/integration/test_external_boot_job_lifecycle.py` |
| Criterion 5 | task-test-not-applicable | live retained-fixture proof; needs the operator host's state, recorded in the PR |
