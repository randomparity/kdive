# 0516 — The private-upload publish holds the PROJECT lock across its PUT by design, and asserts it

## Status

Accepted (2026-07-30)

## Context

ADR-0506 classified 46 `conn.transaction(), advisory_xact_lock(...)` sites into three groups by
who supplies the connection, and closed with a deliberate gap: *"The 18 server-path sites remain
unclassified by this record, by scope. Whoever classifies them inherits the finding above: their
connection is non-autocommit, so the question at each is not 'does it commit' but 'what does its
lock span'."* #1712 is that inheritance, narrowed to the sites where the answer is an object-store
write.

**The site #1712 names is not demoted.** Its title asserts that `services/images/upload.py` holds a
lock across an object-store write under a savepoint on the MCP request path. Checked claim by
claim against the tree, it does not:

- `_publish_under_quota` opens `async with conn.transaction(), advisory_xact_lock(conn,
  LockScope.PROJECT, project)`. Everything `register_private_upload` runs before it is non-DB
  work — `validate_key_component`, `store.head` / `store.get_artifact` via `asyncio.to_thread`,
  a `hashlib` digest, a temp-dir stage, `_validate_staged`. No `conn.execute` anywhere on the
  path.
- Its only caller, `_register_upload` (`mcp/tools/ops/images/upload.py`), opens a fresh
  `pool.connection()` and passes it straight in. `require_role` is pure Python;
  `audit_project_denial` uses its own connection and only on the denial path.
- Checkout issues no SQL of its own: psycopg_pool's `_check_connection` is a no-op unless `check`
  is set, and `db/pool.py` passes no `check`, `configure` or `reset` callback.

So the transaction is real and the lock is scoped to the block. Recording that is half of what
this ADR is for — the next reader should not re-derive it, and should not "fix" a demotion that
is not there.

**What is real is that this is true by accident.** Nothing in the tree pins it. It holds only
because no caller happens to run a query first, and a caller is free to add one. That is exactly
the residual risk ADR-0506 named in its own Consequences — *"a site's group is a property of its
callers, so adding a second caller to a helper can move it between groups without touching the
helper"* — and it is item 3 of #1712's body. Here the consequence of moving group is not a longer
hold on a database lock: `publish_image` writes the image object, so a demoted transaction would
hold the PROJECT lock across the S3/MinIO PUT for the remainder of the request, which is the
ADR-0244 shape. The change that caused it would be one bare `SELECT` in a different module, and
nothing at either site would show.

**The other candidates #1712 names do not have this shape.** Both were checked, and both are
demoted today:

- `mcp/tools/catalog/artifacts/uploads.py` — `spec.project(conn, uid)` reads before the
  `conn.transaction()`, so that block is a savepoint. It spans no external I/O: `presign_put`
  reaches `generate_presigned_url` (`store/objectstore.py`), which is local SigV4 signing with no
  network call. The demotion holds the owner lock to the end of the request and nothing else.
- `mcp/tools/lifecycle/investigations/complete_rootfs_upload.py` —
  `resolve_contributor_investigation` reads first, so `_finalize_locked` is a savepoint too. It
  spans one `store.head`: a bounded metadata call, not a body transfer.

`services/runs/complete_build.py` does hold a lock across a real write under a savepoint, and is
deliberately documented as such. It is not disturbed here.

## Decision

### 1. Assert the top-level transaction at the publish

`_publish_under_quota` calls `require_top_level_transaction(conn, "the private-upload publish")`
immediately before its `conn.transaction()`. This is the established idiom for exactly this
concern — `artifacts/write_lease.py`, `services/allocation/promotion.py` (four call sites),
`reconciler/cleanup/uploads.py`, `reconciler/cleanup/upload_orphans.py` — and it is the one
mechanism ADR-0506 identified as able to observe the property at all, idleness being a runtime
fact about the calling connection that no AST or grep rule can see.

It goes in `_publish_under_quota` rather than at the entry of `register_private_upload` because
the check is only meaningful adjacent to the `transaction()` it guards: everything between the
two is non-DB work today, and a check placed earlier would still pass if a future edit inserted a
read between them.

ADR-0506 rejected asserting at all 46 sites, on the grounds that a blanket assertion trains the
reader that the check is boilerplate. That reasoning selects *for* this site rather than against
it: this is one of the few where the property is load-bearing and currently unpinned.

### 2. The lock spans the PUT by design, not by oversight

The fix is to *pin* the current behaviour, not to shorten the critical section. Holding PROJECT
across the object-store write is what makes the per-project quota fail-closed: `_project_usage`
counts live rows and sums their object bytes, and `publish_image` adds the row and the object.
Release the lock between them and two concurrent uploads both read the pre-write total and both
pass a cap they jointly breach. `test_concurrent_uploads_cannot_both_pass_the_cap` pins that
outcome today.

The alternative shapes — reserve the bytes in a row first and publish outside the lock, or admit
optimistic over-admission and reconcile — are real designs, and each is a larger change to the
quota model than #1712 scopes. The lock *duration* concern they answer is filed separately as
#1726, which this record leaves as the open question rather than pre-empting.

### 3. The presign site records why it is not guarded

`mcp/tools/catalog/artifacts/uploads.py` gets a comment at its `conn.transaction()` stating that
the savepoint demotion is known, that the block spans only local signing, and that adding
object-store I/O inside it would change the answer. Its behaviour is unchanged. Without the note
the next reader re-derives the presign call graph to find out whether the site is judged or
merely unexamined — the same cost ADR-0506 paid to write, and the reason it recorded its immune
and safe-by-pattern groups with a reason each instead of leaving them silent.

### 4. This closes ADR-0506's deferred scope for this site only

Two of the 18 are now classified — `services/images/upload.py` (top-level, asserted) and
`mcp/tools/catalog/artifacts/uploads.py` (demoted, spans no I/O) — plus
`complete_rootfs_upload.py` recorded above as demoted over a bounded `head`. The remaining
server-path sites stay unclassified, and the async worker path is out of scope entirely: #1725
covers `jobs/handlers/control/diagnostic_sysrq.py` and `capture_traffic.py`, which do hold a lock
across a real `put_artifact`, on connections the worker's `set_autocommit(True)` dispatch makes
top-level.

## Consequences

- `register_private_upload` now requires a transaction-free connection and says so in its `Args`
  and `Raises`. Every caller satisfies it today — the MCP handler passes a fresh pooled
  connection, the tests pass autocommit ones. A future caller that reads first gets a
  `RuntimeError` naming the fix, at the call that would otherwise have silently extended the lock
  over the PUT.
- The failure mode changes from silent to loud, not from broken to correct. Nothing that works
  today stops working; a change that would have degraded the lock now fails the request instead.
  On the MCP path that surfaces as a `RuntimeError` escaping `_register_upload`'s
  `except CategorizedError`, so it becomes an internal error rather than a typed envelope — which
  is the right shape for a programming error in the caller, not a condition an agent can act on.
- Two tests are added to `tests/services/images/test_upload.py`, and they are the first there to
  use a **non-autocommit** connection — the shape ADR-0506 found the promotion tests were missing.
  The 19 that preceded them connect with `autocommit=True`, where `transaction_status` is `IDLE`
  and the trap cannot arise, which is why nothing in the suite would have caught a caller
  dirtying the connection. New tests about lock lifetime or commit boundaries in this file must
  use `_connect_pooled_shape`, not `_connect`.
- The guard test was mutation-verified: with the `require_top_level_transaction` line deleted it
  fails with `DID NOT RAISE RuntimeError`, and it is the only test that fails, so no other test is
  silently pinning it.
- The positive-control test reads `pg_locks` from a **second** connection filtered to the
  publish's backend pid, for the reason ADR-0506 gives: probing from the connection under test
  would itself issue the statement that opens the transaction being measured. It asserts zero
  advisory locks held after `register_private_upload` returns — the property the guard exists to
  keep true, rather than the guard itself.
- No schema, migration, config, tool schema, RBAC rule or agent-visible string changes. No new
  dependency.
- The PROJECT lock still spans N `store.head` calls plus a multi-GiB PUT. That is accepted here
  and tracked as #1726; this record makes the span deliberate and pinned, which is the
  precondition for changing it safely later.

## Considered & rejected

- **Hoist nothing and document the requirement in the docstring.** The cheapest option, and the
  one the tree already relies on implicitly. Rejected for the reason #1687 established and
  ADR-0506 restated: the property is invisible at the call site and a sentence cannot detect when
  it stops holding. The promotion sweep's docstring claimed "each in its own committed
  transaction" while doing the opposite, and had for some time.
- **Restructure so the lock is not held across the PUT**, as #1712's step 2 offers as an
  alternative. It is the more thorough fix for lock duration and it is a different change: the
  quota check and the publish would need to be split by a reservation row or an admit-then-
  reconcile scheme, changing the fail-closed guarantee that
  `test_concurrent_uploads_cannot_both_pass_the_cap` pins. Filed as #1726 rather than folded into
  a guard-and-record change.
- **Guard `mcp/tools/catalog/artifacts/uploads.py` too.** It would fire on every legitimate call:
  the read that dirties the connection is `spec.project`, which resolves the owner the lock key is
  derived from, so it cannot move inside the lock it precedes. #1697's conclusion applies exactly
  — a guard that raises on every valid invocation is worse than none. A comment records the
  judgement instead.
- **Hoist `spec.project` out of the presign handler so its transaction is genuinely top-level.**
  Possible, by taking a second connection for the resolve. It buys nothing: the block spans no
  external I/O, so the only effect of the demotion is a longer hold on the owner lock within one
  request, and it would add a pool checkout per presign call.
- **Assert at the entry of `register_private_upload` instead.** Reads better and covers the public
  function rather than a private helper. Rejected in §1: it would pass while an edit between the
  two functions demoted the transaction, guarding the wrong instant.
- **Make pooled connections autocommit**, which removes the trap tree-wide. ADR-0506 rejected this
  as out of scope for a bugfix — it changes the semantics of 195 `conn.transaction()` sites,
  including paths that rely on an enclosing transaction rolling back a multi-statement failure —
  and nothing since has changed that. It remains the right question for its own ADR with its own
  audit.
- **Fix the issue as titled.** The title asserts a live demotion at
  `services/images/upload.py`. Verified absent, twice and independently. Acting on it would have
  meant hoisting reads that are already hoisted, and the finding — that the property holds by
  accident — would have been lost behind a no-op diff.
