# 0629 — Worker-fenced System mutation-obligation discharge

## Status

Accepted (2026-09-07)

## Context

`remote_module_attempt_obligations` grants `kdive_worker` and `kdive_reconciler` `SELECT`
only (`../../src/kdive/db/schema/0126_remote_module_attempt_obligations.sql:230-232`), and no
later migration widens either. The bulk terminal-escape discharge
`RemoteModuleAttemptObligationRepository.discharge_system_mutation_obligations` nevertheless
issues a direct `UPDATE` on that table, and both of its callers run under one of those two
roles: the teardown job handler through
`../../src/kdive/jobs/handlers/system_reclaim.py:110-113`, and the reconciler's
authority-System teardown repair through `../../src/kdive/reconciler/repairs/jobs.py:98-103`.
`systems.teardown` therefore fails with `permission denied for table
remote_module_attempt_obligations` (#2302).

ADR-0609 already answers this shape for the per-attempt worker writes: migration 0134 defines
`public.commit_worker_remote_module_evidence`, a `SECURITY DEFINER` function granted to
`kdive_worker`, so the worker reaches its own writes without holding table-level `UPDATE`. The
bulk discharge cannot reuse it: it is per-System rather than per-attempt, and its second caller
is the reconciler, which holds no job, attempt, or operation nonce to fence on.

The discharge's serializing fence is a caller obligation today: both call sites hold
`advisory_xact_lock(conn, LockScope.SYSTEM, system_id)`, the same fence ADR-0605 verification
takes at `../../src/kdive/services/remote_module_attempt_preparation.py:80`.

## Decision

We will route the bulk discharge through
`public.discharge_system_mutation_obligations(uuid)`, a `SECURITY DEFINER` function whose body
is the one `UPDATE` it exists for, gated on `kdive_worker`-or-`kdive_reconciler` role
membership and granted `EXECUTE` to exactly those two roles. Neither role gains any table
privilege. The serializing System lock stays with the Python caller.

## Consequences

- Both roles can discharge open mutation obligations for one System and can do nothing else
  through this surface: the function writes `mutation_discharged_at` and the fixed reason
  `'terminal_escape'`, on rows of one System where `mutation_discharged_at IS NULL`. The
  write-once trigger `remote_module_attempt_obligations_write_once` still governs the write.
- The function runs as its owner, so it carries `SET search_path = ''` and fully qualifies
  every name, matching 0134.
- The role gate uses `pg_has_role(session_user, …)`, which is true for a superuser against
  every role, so every test arm needs a real `LOGIN` principal.
- Granting the reconciler is a consequence of rerouting one shared method: without it the
  sibling call site would trade `permission denied for table` for `permission denied for
  function`.
- The raw privilege string stops reaching the agent because the call now succeeds. Mapping
  `psycopg.errors.InsufficientPrivilege` in the shared worker exception path (#2302's second
  half) stays open and is unaffected by this record.

## Considered & rejected

- **Grant `UPDATE` on the table to `kdive_worker` and `kdive_reconciler`.** judgment: a
  table-level write lets either role rewrite any column of any obligation row, including the
  terminal and restored evidence whose write-once triggers exist to protect it. That is the
  fence ADR-0609 built, removed to fix one statement.
- **Reuse `commit_worker_remote_module_evidence`.** verified: that function requires a job id,
  credential hash, job attempt, run id, and operation nonce, and matches a `running` job with a
  live lease (`../../src/kdive/db/schema/0134_remote_module_worker_evidence.sql:1-66`). The
  discharge is per-System and bulk, and its reconciler caller
  (`../../src/kdive/reconciler/repairs/jobs.py:98-103`) holds none of those five facts.
- **Move the discharge to a role that already holds `UPDATE`.** verified: only `kdive_server`
  holds it (`0126:230-232`), and the discharge is ordered after `provisioner.teardown`, which
  only the worker observes (`../../src/kdive/jobs/handlers/systems.py:717-736`). The reconciler
  is no alternative either — `0126:231-232` revokes and re-grants it `SELECT` alone.
- **Take the System advisory lock inside the function.** verified: the fence this discharge
  shares with ADR-0605 verification is the Python helper's key,
  `blake2b(b'system\x00' || uuid)` (`../../src/kdive/db/locks.py:79-91`), while every in-schema
  lock uses the disjoint `hashtextextended('kdive:system:' || id, 2125)` key space
  (`../../src/kdive/db/schema/0122_external_boot_authority.sql:413`). An in-body lock would
  serialize against nothing this write needs.
- **Do nothing and let the caller retry.** verified: the first attempt commits the terminal
  System state before the failing discharge
  (`../../src/kdive/jobs/handlers/systems.py:700-706`), and `systems.teardown` then returns
  `torn_down` from its terminal short-circuit without enqueueing a job
  (`../../src/kdive/mcp/tools/lifecycle/systems/admin.py:451-462`). The retry reports success
  and no later job ever discharges the obligations.
