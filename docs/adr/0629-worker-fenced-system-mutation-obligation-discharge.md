# 0629 — Worker-fenced System mutation-obligation discharge

## Status

Accepted (2026-09-07)

## Context

`remote_module_attempt_obligations` grants `kdive_worker` and `kdive_reconciler` `SELECT`
only (`../../src/kdive/db/schema/0126_remote_module_attempt_obligations.sql:230-232`), and no
later migration widens either. `RemoteModuleAttemptObligationRepository.discharge_system_mutation_obligations`
(`../../src/kdive/db/remote_module_attempt_obligations.py:305-325`) nevertheless issues a
direct bulk `UPDATE` on that table, and it is reached from two role families:

- `kdive_worker` and `kdive_reconciler`, through
  `../../src/kdive/jobs/handlers/system_reclaim.py:111` — the System-teardown reclaim path, and
  the one #2302 reports failing with `permission denied for table`. Three call sites reach it
  with `discharge_mutation_obligations=True`: `jobs/handlers/systems.py:730`,
  `jobs/handlers/system_authority.py:223`, and `reconciler/repairs/jobs.py:98`. A fourth,
  `jobs/handlers/external_boot/lifecycle.py:1073`, passes `False` and discharges through 0147's
  `finalize_external_boot_authority_teardown` instead, so it is unaffected.
- `kdive_server`, through `../../src/kdive/db/external_boot_activations.py:552` and `:886`,
  which discharge on the `abandoned` and `recovery_failed` activation edges beside an `UPDATE`
  on `external_boot_activations`, a table granted to `kdive_server` alone. That role holds
  `UPDATE` on the obligations table too (`0126:230`), so those two paths work today.

The runtime roles hold no memberships and do not inherit
(`../../src/kdive/db/schema/0104_worker_fence_roles.sql:8-25`), so a fix gated on
`kdive_worker` membership excludes `kdive_server` outright.

Two in-repo precedents already answer this shape. Migration 0132 splices the identical
`terminal_escape` `UPDATE` into `commit_external_boot_authority_result`, an existing
`SECURITY DEFINER` function, under a verbatim-matching premise: "Workers intentionally have
only SELECT on the obligations table, so compose the write into that function rather than
widening worker table privileges"; 0147 does the same for
`finalize_external_boot_authority_teardown`. And ADR-0609's Python-side precedent is a
**variant-method split**, not a reroute: `worker_record_terminal_evidence`,
`worker_record_restored_evidence`, and `worker_discharge_reap_obligation`
(`remote_module_attempt_obligations.py:398`, `:420`, `:442`) route through
`public.commit_worker_remote_module_evidence` while the server-role siblings beside them keep
their direct writes.

The System-teardown reclaim path has no enclosing `SECURITY DEFINER` function to splice into —
`reclaim_system_core_after_provider_teardown` is plain Python — so 0132's composition is not
available here and the split is.

## Decision

We will add `public.discharge_system_mutation_obligations(uuid)`, a `SECURITY DEFINER`
function whose body is the one bulk `UPDATE` it exists for, gated on
`kdive_worker`-or-`kdive_reconciler` membership and granted `EXECUTE` to exactly those two
roles. A new `worker_discharge_system_mutation_obligations` repository method calls it, and
only the teardown reclaim path switches to that method. The shared
`discharge_system_mutation_obligations` and its two `kdive_server` call sites are untouched.

## Consequences

- The worker and reconciler can discharge open mutation obligations for one System and can do
  nothing else through this surface: the function writes `mutation_discharged_at` and the fixed
  reason `'terminal_escape'` on rows of one System where `mutation_discharged_at IS NULL`.
  Neither role gains a table privilege.
- The `kdive_server` activation edges keep their existing direct write and their existing
  behaviour, so this change cannot regress them.
- The function runs as its owner, so it carries `SET search_path = ''` and fully qualifies
  every name, matching 0134.
- The repository now has two methods for one write, as it already does three times over for the
  ADR-0609 evidence writes. The cost is that a future caller must pick the one matching its
  role; the alternative — one method that works for every role — is what the caller inventory
  above shows cannot be built from a single grant.
- The gate uses `pg_has_role(session_user, …)`, which is true for a superuser against every
  role, so every test arm needs a real `LOGIN` principal.
- **This function fences one level weaker than the 0134 precedent it follows, deliberately.**
  `commit_worker_remote_module_evidence` gates on the same `pg_has_role` check *plus* an active
  `worker_incarnations` row matched by credential hash and a `running` job row with a live
  lease, bound to the exact attempt tuple. 0152 gates on role membership alone because the
  fence is not satisfiable at every call site: the two job-handler sites
  (`jobs/handlers/systems.py:730`, `jobs/handlers/system_authority.py:223`) do hold a job and
  attempt, but the reconciler repair site (`reconciler/repairs/jobs.py:98`) sweeps a batch of
  candidate Systems with no job, attempt, or incarnation credential of its own. A fence only
  two of the three could satisfy cannot be made mandatory in the function all three share. So
  any process
  holding a worker or reconciler login can discharge any System's open mutation obligations,
  where 0134 requires that process to also hold a live lease on the matching job. The write is
  bounded to that one idempotent terminal-escape statement, which is why the residual is
  accepted rather than closed with lease plumbing this path cannot supply.
- Obligations already leaked by #2302 on live Systems are not repaired by this change. They
  stay open, and `retained_owners` (`remote_module_attempt_obligations.py:513-525`) keeps their
  volumes out of the reaper. Remediating those rows is separate follow-up work, owned by the
  campaign that dispatched this fix; this record stops new leaks and repairs none.

## Considered & rejected

- **Reroute the shared `discharge_system_mutation_obligations` through the function.** verified:
  `external_boot_activations.py:552` and `:886` call it under `kdive_server`, which
  `0104_worker_fence_roles.sql:8-25` establishes is a member of no role, so it would fail both
  the `EXECUTE` grant and the in-body gate — trading a working `UPDATE` for `permission denied
  for function` on two paths that work today.
- **Grant the two roles table-level `UPDATE`.** verified: the write-once trigger
  `reject_remote_module_attempt_rewrite` (`0126:184-222`) blocks only *rewrites* — each of its
  five guards is conditioned on the old value being non-null — so a table grant would let
  either role make the unfenced **first** write of `terminal_operation`, the restored evidence,
  `reap_opened_at`, and `reap_discharged_at`. Those first writes are exactly what 0134's
  job-lease fence governs, and the trigger does not cover them.
- **Grant `UPDATE (mutation_discharged_at, mutation_discharge_reason)`, column-scoped.**
  verified: column-scoped grants are established here (`0118:3`, `0115:75`), and this one does
  escape the trigger objection above. It was excluded by the operator's scope decision for
  #2302, which directed `EXECUTE` on a function rather than table-level write; independently, it
  still admits any of the three reasons the column's CHECK allows (`0126:69-73`) on any System,
  where the function fixes the reason to `'terminal_escape'`.
- **Splice the write into an existing definer function, as 0132 and 0147 do.** verified: those
  two both had one — `commit_external_boot_authority_result` and
  `finalize_external_boot_authority_teardown`. The ordinary teardown reclaim path has none:
  `../../src/kdive/jobs/handlers/system_reclaim.py:98-113` is plain Python called from four
  sites.
- **Take the System advisory lock inside the function.** verified: the fence this discharge
  shares with ADR-0605 verification is the Python helper's key,
  `blake2b(b'system\x00' || uuid)` (`../../src/kdive/db/locks.py:79-91`), which
  `../../src/kdive/services/remote_module_attempt_preparation.py:80` takes, while every
  in-schema lock uses the disjoint `hashtextextended('kdive:system:' || id, 2125)` key space
  (`../../src/kdive/db/schema/0122_external_boot_authority.sql:413`). An in-body lock would
  serialize against nothing this write needs, and under READ COMMITTED the
  `mutation_discharged_at IS NULL` predicate is re-checked on each row the statement blocks on,
  so a concurrent discharge cannot double-write.
- **Do nothing and let the caller retry.** verified: the first attempt commits the terminal
  System state before the failing discharge
  (`../../src/kdive/jobs/handlers/systems.py:700-706`), and `systems.teardown` then returns
  `torn_down` from its terminal short-circuit without enqueueing a job
  (`../../src/kdive/mcp/tools/lifecycle/systems/admin.py:451-462`). The retry reports success
  and no later job ever discharges the obligations.
