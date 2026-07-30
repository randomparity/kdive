# 0506 — Savepoint demotion is checked per candidate at a sweep loop, not audited once

## Status

Accepted (2026-07-30)

## Context

`conn.transaction()` opens a real transaction only on an **idle** connection. On one already
in a transaction it opens a `SAVEPOINT`, and releasing a savepoint commits nothing and
releases no `pg_advisory_xact_lock`. A non-autocommit connection enters that state invisibly:
one bare `execute` outside any `transaction()` block starts a transaction that lives until the
pool returns the connection. So a block meaning *"commit this, and hold a lock only while I
do"* becomes *"defer this to my caller's commit, and hold the lock until then"* — opposite
behaviours with no observable difference at the call site. #1687 (PR #1696) hit this as a live
defect and added `require_top_level_transaction` (`src/kdive/db/locks.py:100`) to make it
loud; #1697 asked for the rest of the tree.

`rg -n 'conn\.transaction\(\).*advisory_xact_lock' src/` returns **46** sites on `main` at
`a79bc0a33`. That number is a proxy, not a census: it is single-line-only, so the multi-line
`async with (conn.transaction(), advisory_xact_lock(...), ...)` in this file's own
`_reap_one` is not among the 46. Grep cannot answer the question anyway — whether a given
`conn.transaction()` is real depends on what ran earlier on that connection, which is a
dataflow property of the *caller*.

What decides it is not the call site but **who supplies the connection**. Three suppliers
exist, and each of the 46 sites belongs to exactly one:

- **Worker job handlers — immune by construction.** `jobs/worker.py:239-244` wraps handler
  dispatch in `set_autocommit(True)`, so every `conn.transaction()` a handler opens BEGINs a
  real transaction regardless of what ran before it. 22 of the 46 are here
  (`jobs/handlers/control/{control,diagnostic_sysrq,capture_traffic}.py`,
  `jobs/handlers/systems.py`, `jobs/handlers/artifacts/vmcore.py`,
  `jobs/handlers/runs/install.py`, `jobs/handlers/console/console_rotate.py`, and
  `artifacts/write_lease.py` — whose only caller is `vmcore.py:289`, on that same connection;
  it asserts the requirement anyway, at `write_lease.py:82`). The issue cited
  `finalize_capture` in `vmcore.py` as a known-live instance; on the autocommit dispatch
  connection it is not one. That claim in the issue body is wrong, and correcting it is part
  of what this record is for — the next reader should not re-derive it.
- **Sites reached on a freshly taken connection — safe by pattern.** `reconciler/loop.py:605`
  opens a fresh `pool.connection()` per repair and the repair's candidate SELECT is wrapped in
  `conn.transaction()` (`reconciler/repairs/systems.py:41` is the reference shape), so each
  per-candidate block starts idle. 5 sites: `reconciler/repairs/systems.py` (3),
  `reconciler/cleanup/uploads.py` (1), and `jobs/worker.py:369` (1) — that last one is *not*
  covered by the autocommit dispatch above, because `_fail_job_and_run` is reached from
  `worker.py:161` and `worker.py:249`, each of which takes its own `pool.connection()` and
  runs only pure-Python code before the lock. None of the 5 asserts the requirement. The one
  reconciler site that does, `reconciler/cleanup/upload_orphans.py:490`, is not among the 46
  at all: it locks with `try_advisory_xact_lock`, which the grep does not match — another way
  the 46 undercounts.
- **The MCP server request path — not judged here.** `db/pool.py:52` sets no `autocommit`, so
  `pool.connection()` in a tool handler yields a **non-autocommit** connection, and a handler
  that reads before it locks demotes its own transaction. 18 sites, across
  `mcp/tools/lifecycle/**`, `mcp/tools/catalog/artifacts/uploads.py`,
  `services/investigations/**`, `services/runs/complete_build.py`,
  `services/debug/lifecycle.py`, `services/images/upload.py`,
  `services/allocation/renew.py`, and `services/allocation/admission/core.py:196` (reached
  from `services/allocation/admission/request.py:107` after two unlocked reads). These sites
  are owned by concurrent work in the same campaign and are **not** classified as safe by
  this record. What is established about them: `pool.connection()` commits on clean exit, so
  a demoted write is committed late rather than lost — the lock is held *longer* than the
  block, never released early. That is the same category the issue assigns to `vmcore.py`:
  live, and load-bearing exactly where a lock would span an object-store write.

That leaves **one** file where the demotion is not a longer hold but a broken contract:
`services/allocation/promotion.py`. Both of its sweeps candidate-SELECT with a bare
`conn.cursor()` outside any transaction (`:84`, `:407`), dirtying the pooled connection for
the whole pass; `_promote_one`'s own `SELECT project` (`:119`) dirties it a second time even
if the first were fixed. Every subsequent `conn.transaction()` is a savepoint, so no
candidate's locks are ever released. Measured on the unfixed tree with the test added here,
candidate 2 of a two-candidate pass enters holding **3** advisory locks — candidate 1's
PROJECT, RESOURCE and ALLOCATION. It then takes PROJECT on top of them. `db/locks.py:34-37`
documents the total order `PROJECT → RESOURCE → ALLOCATION`, and a concurrent `admit` takes
PROJECT (`admission/core.py:196`) then RESOURCE (`:382`). Two holders acquiring the same pair
in opposite orders is an ABBA deadlock. It also falsifies the module's own docstring
("attempts each in its own committed transaction").

## Decision

Fix `promotion.py`, and check the property at the loop rather than describing it.

Wrap both candidate SELECTs in `conn.transaction()`, matching
`reconciler/repairs/systems.py:42`, and move `_promote_one`'s `SELECT project` **inside** the
transaction it precedes. That read derives the PROJECT lock key so it cannot be taken under
that lock; it takes no advisory lock, so it does not enter the total order.

Call `require_top_level_transaction` **inside each sweep's `for` loop, outside the
`try`** — once per candidate, not once per pass. Per candidate, because the state being
checked is produced by the *previous* iteration: a still-open transaction means the previous
candidate's locks are still held and this one is about to invert the order. Outside the
`try`, because both loops carry an `except Exception: log and continue` that isolates one bad
candidate from its siblings; a savepoint-demoted connection is a fault of the pass, not of the
candidate, and must abort the pass rather than be logged 200 times and retried next tick.

Prove each of the four assertions by deleting it and watching a test fail, and prove the
hazard rather than the guard. The four are the entry and per-candidate guard of each sweep;
each was removed on its own and each took exactly one test red with it, so no guard is
carried by a test that was really pinning a different one.

The lock tests read `pg_locks` from a **second** connection, filtered to the sweep backend's
pid — probing from the sweep's own connection would issue the statement that opens the
transaction under test. They compare lock *identities*, not counts: `pg_locks` exposes an
advisory key as `(classid, objid)`, which `_advisory_lock_oids(_lock_key(scope, key))` maps
back to a `LockScope`, so the test can assert that the locks still held when candidate 2 is
refused are exactly candidate 1's PROJECT, RESOURCE and ALLOCATION. A count would show
accumulation; only the identities show the inversion. Measured at the entry of each candidate
on the fixed tree the held count is `[0, 0]`; on the unfixed tree it is `[0, 3]`.

## Consequences

- The promotion sweep and the queue-timeout reaper now require a transaction-free connection
  and say so in their `Args`/`Raises`. The reconciler already satisfies this; a future caller
  that does not gets a `RuntimeError` naming the fix instead of silent lock accumulation.
- A demoted connection aborts the whole pass. `_run_repair_plan` catches it, records the
  repair as failed for the pass, and the next tick retries on a connection the pool has reset
  — so the failure is visible in the repair-failure list rather than absorbed into a
  per-candidate warning. The pass's count is lost with it: candidates that committed before
  the abort keep their grants, but `_run_repair_plan` reports the repair's count as 0, so the
  promoted total under-reports on exactly the passes that fail. Accepted — the grants are
  durable and the failure is already surfaced; a partial count would need a mutable
  accumulator threaded through both sweeps to fix an observability gap on an error path.
- The seven tests added to `tests/services/allocation/test_promotion.py` are the first in that
  file to run on a non-autocommit connection. The 16 that preceded them use autocommit, where
  the trap cannot appear; that is why the defect survived them. New tests for lock lifetime or
  commit boundaries in this area must use `_pooled_conn`, not `_conn`.
- 22 sites are recorded as immune and 5 as safe-by-pattern, with the reason in each case, so
  they read as judged rather than unexamined. Both judgements are conditional on their
  supplier: if `jobs/worker.py:240` stopped setting `set_autocommit(True)`, or a repair
  stopped opening a fresh connection, 27 sites change class at once and nothing here would
  fail. That coupling is the residual risk this record does not remove — and note that a
  site's group is a property of its *callers*, so adding a second caller to a helper can move
  it between groups without touching the helper. `artifacts/write_lease.py` is the one to
  watch: it sits in the immune group only because `vmcore.py:289` is its sole caller, and its
  own assertion is what would catch a second one.
- The 18 server-path sites remain unclassified by this record, by scope. Whoever classifies
  them inherits the finding above: their connection is non-autocommit, so the question at each
  is not "does it commit" but "what does its lock span".

## Considered & rejected

- **A structural guard — lint for `advisory_xact_lock` inside a `conn.transaction()` on a
  connection not proven idle**, as the issue suggested. Rejected: idleness is a property of
  the calling *connection* at runtime, set by statements in a different function and often a
  different module (the worker's `set_autocommit`, three frames up). No AST or grep rule can
  see it. The issue anticipated this ("a grep-style guard may not be sufficient"); the runtime
  assertion is the version of the same idea that can actually observe the state.
- **Assert `require_top_level_transaction` at all 46 sites.** It would fire at 22 sites that
  cannot fail and 18 that another change owns, adding a line of ceremony per site and a
  merge conflict per site, to guard a property those sites do not have. Worse, a blanket
  assertion trains the reader that the check is boilerplate, which is the opposite of what
  makes it useful at the two sites where it is load-bearing.
- **Make pooled connections autocommit** (`create_pool(kwargs={"autocommit": True})`), which
  would make the trap unreachable everywhere at once. Rejected as far out of scope for a
  bugfix: it changes the transaction semantics of every one of the 195 `conn.transaction()`
  call sites in the tree, including paths that today rely on an enclosing transaction rolling
  back a multi-statement failure. It is the right question for a separate ADR with its own
  audit, not a side effect of fixing two sweeps.
- **Assert inside `_promote_one` / `_reap_one` instead of in the loop.** Simpler to read and
  it covers direct callers, but both are private and called only from their loop, and the
  loop's `except Exception` would swallow the `RuntimeError` into a per-candidate warning —
  turning a pass-wide fault into a sweep that quietly promotes nothing forever.
- **Keep the bare candidate SELECT and give each candidate a fresh connection from the pool.**
  It removes the accumulation, but takes a pool connection per candidate on a pass that can
  have hundreds, and the reconciler's pool is sized for one connection per repair. Wrapping
  the SELECT costs one transaction per pass and leaves the connection model unchanged.
- **Document the requirement in the docstring only.** This is what the tree already did — the
  sweep's docstring has always claimed "each in its own committed transaction" while doing the
  opposite. The claim was not wrong when written; it stopped being true invisibly. A sentence
  cannot detect that, which is the whole finding of #1687.
