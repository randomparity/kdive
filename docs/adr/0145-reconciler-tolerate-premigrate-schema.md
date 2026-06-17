# ADR 0145 — Console-hosting tolerates a not-yet-migrated schema at startup

- **Status:** Accepted
- **Date:** 2026-06-16
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0095](0095-reconciler-remote-console-collector.md)
  (the single-leader remote console-hosting loop, its `RunningSystems` port, and the
  `DbRunningRemoteSystems` Postgres query this ADR narrows the failure contract of).
- **Issue:** [#498](https://github.com/randomparity/kdive/issues/498)

## Context

On a clean `helm install`, the bundled `migrate` Job is a `post-install,post-upgrade`
**hook**, so Helm starts the `reconciler` Deployment (a normal resource) **before** the
schema exists. During that window the reconciler's console-hosting tick runs
`DbRunningRemoteSystems.list_running()`, whose query

```
SELECT s.id FROM systems s JOIN allocations a ON … JOIN resources r ON … WHERE …
```

raises `psycopg.errors.UndefinedTable` (`relation "systems" does not exist`).

The tick already contains the error: `ConsoleHostingLoop.tick` wraps the body in
`except Exception` and logs `console hosting tick failed; retrying next tick`, and the
existing `test_tick_survives_a_running_systems_query_error` pins that the tick does not
propagate. So the reconciler process is **not** crashed by this tick. The defect that is
real and reproducible is what the tick emits during the normal post-install window: a
`WARN`-level message **with a full `UndefinedTable` traceback on every tick** until
`migrate` completes. During a deploy that reads as a fault — it is indistinguishable from
a genuine transient DB error, and it is the same class as the historical hook-ordering
traps in the deploy notes.

A missing catalog at startup is not a transient failure to be logged-and-retried with a
stack trace; it is an **expected, benign, self-healing** condition: there are simply no
running Systems to host yet because the catalog does not exist yet.

## Decision

Treat a not-yet-migrated schema as "no running Systems" at the **data-access boundary**,
`DbRunningRemoteSystems.list_running`:

- Catch `psycopg.errors.UndefinedTable` from the running-Systems query and return an
  empty `set()`.
- Log it at **`DEBUG`** with a plain message (`"running-systems query found no schema yet
  (pre-migration); treating as none running"`) and **no traceback** — operators can see
  it when debugging, but it does not surface as a deploy-time warning.

The tick then proceeds with an empty running set: it opens no collectors, hosts nothing,
and re-runs next tick. Once `migrate` creates the schema the same query returns the
running Systems and hosting begins — no operator action.

**Falsifiable success criterion.** The fix's testable outcome is that the console-hosting
tick emits **no `WARN`-level record** (and no `UndefinedTable` traceback) while the schema
is absent — asserted directly with a log-capture test, not merely "the tick does not
raise" (the existing `test_tick_survives_a_running_systems_query_error` already pins the
no-raise property, and a fix that only re-confirmed it would be vacuous).

**On the reported restart.** The issue reports `restartCount=1`. As the Context notes, the
tick's existing `except Exception` already contains this error, so the tick does **not**
crash the reconciler process — this change cannot, and does not claim to, eliminate a
process restart, because the tick was never the thing exiting the process. The single
restart the reporter observed is not attributed to this tick; the most likely cause is
ordinary startup timing (the liveness probe's window while `build_reconciler_console_hosting`
and provider-discovery registration run before the reconcile loop's heartbeat ticker
starts), which self-heals on the restart once migrate completes. Confirming and, if
warranted, fixing that startup-timing path is **out of scope** for this ADR and left to a
separate investigation; this ADR fixes the misleading deploy-window log noise, which is
the part of #498 the evidence and traceback actually pin to the console-hosting tick.

The catch is scoped to **`UndefinedTable` only**. Any other query error (a genuinely
down or broken DB) still propagates to the tick's existing `except Exception`, keeping
its WARN-with-traceback — those are not benign and must stay loud.

## Consequences

- The post-install / post-upgrade window is quiet: the reconciler no longer logs an
  alarming `UndefinedTable` traceback while waiting for the migrate hook.
- The contract is narrow and honest: only the precise "schema absent" error is treated as
  benign; every other failure keeps its existing loud, retried handling.
- `list_running` returning `set()` for both "schema absent" and "schema present, nothing
  running" is correct for its one consumer — the hosting loop hosts nothing in both cases.
- Demoting the signal to `DEBUG` quiets the *transient* post-install race, not a broken
  deploy. A schema that stays absent because the migrate hook **failed** is surfaced
  independently of the reconciler: the `migrate` Job fails and the Helm release reports
  failure (a `post-install` hook failure fails the install). The reconciler's `/readyz`
  is **not** the backstop here — its Postgres check is a bare `SELECT 1`, which succeeds
  against a connected-but-unmigrated database — so the reconciler-side log going quiet
  does not remove the only signal of a failed migrate; the Job/release failure is the
  authoritative one, and only the normal, self-healing startup window goes quiet.
- The fix is at the query the issue's evidence names. The reconciler's repair passes
  (`_run_repair_plan`) already isolate each repair and would log their own pre-migration
  warnings, but they are out of scope here: the issue's traceback and title are
  console-hosting-specific, and broadening the benign-`UndefinedTable` treatment into the
  core repair loop is a larger failure-contract change best made under its own ADR if the
  deploy-window repair-pass noise is judged worth quieting.

## Considered & rejected

- **Catch `UndefinedTable` in `ConsoleHostingLoop.tick` (or `_host_running_systems`)
  instead of the query.** The loop is provider-neutral and has no business knowing that
  one `RunningSystems` implementation is backed by a SQL table that may not exist. The
  knowledge "this query targets a table that the migrate hook creates later" belongs to
  the Postgres-backed `DbRunningRemoteSystems`, not the loop. A test-double
  `RunningSystems` never raises `UndefinedTable`.
- **A startup gate that blocks the reconciler until the schema exists.** Heavier, adds a
  new readiness dependency and a polling loop, and is redundant with the loop's existing
  retry-next-tick design — the tick is *already* meant to be resilient; it just needs the
  one expected condition reclassified as benign.
- **Broaden the benign treatment to every reconciler DB op now.** Out of scope (see
  Consequences); the repair loop already contains its failures, and widening the contract
  there without evidence it is needed is premature.
