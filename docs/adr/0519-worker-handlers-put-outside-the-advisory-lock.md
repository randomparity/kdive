# 0519 — Worker handlers write their objects outside the advisory lock

## Status

Accepted (2026-07-30)

## Context

Three job-worker handlers held a `pg_advisory_xact_lock` across a real object-store write:

| site | scope | what was inside the lock |
|---|---|---|
| `control/capture_traffic.py` `_store_capture` | `RUN` | one PUT of a pcap bounded by the payload's `max_bytes` |
| `control/diagnostic_sysrq.py` `_store_capture` | `SYSTEM` | one PUT of a redacted console dump |
| `console/console_rotate.py` `_rotate_under_lock` | `SYSTEM` | a sidecar GET plus an **unbounded loop** of part PUTs |

A transaction-scoped advisory lock releases only when its transaction ends, so each of these
holds for as long as the store takes. Under a slow or retrying store, a critical section written
to serialise a short read-check-then-write instead serialises an upload. `SYSTEM` is the
contended scope — teardown, provision, revert and console rotation all take it — so
`console_rotate` bounds every one of those by N object-store round-trips, N being however many
64 KiB parts the console grew since the last rotation.

[0516](0516-private-upload-holds-the-project-lock-across-its-put-by-design.md) established that
these spans are real rather than apparent: the worker's `_run_handler` sets
`set_autocommit(True)` before dispatch, so each `conn.transaction()` block is a genuine
top-level transaction and the lock provably covers the whole PUT. 0516 scoped these three
handlers out of its own decision and named #1725 as their owner. This is that decision.

The obvious restructure — release the lock before the PUT — is not a free hoist, for two
reasons this repository's shape makes sharp.

**The lock is what makes insert-if-absent atomic.** Each site reads a guard (the job's cancel
state, or the System's `SystemState`) and probes for an existing `artifacts` row, then writes.
Split the write out and both facts can change underneath it.

**And an unregistered object is permanent.** Every System-artifact reclaim in this codebase is
**row-driven**: `_reclaim_console_artifacts` and `_reclaim_sysrq_artifacts` in
`jobs/handlers/systems.py` select `object_key` *from the `artifacts` rows* and delete those keys.
`artifacts.object_key` carries no uniqueness constraint outside the `owner_kind =
'investigations'` partial index from schema 0076, so nothing at the database layer would catch a
duplicate either. An object written with no row is therefore invisible to the sweep that exists
to reclaim it, forever — which is a worse outcome than the lock span this change set out to fix.

## Decision

Each of the three sites becomes three phases:

1. a short locked transaction that reads the guard and probes insert-if-absent;
2. the object-store PUT (or PUTs), **unlocked**;
3. a short locked transaction that re-reads the guard, re-probes insert-if-absent, and inserts
   the row(s) plus the audit event.

Both locked phases are database-only, so store latency no longer bounds either.

Phase 1 is not an optimization. Without it, an at-least-once *retry* would PUT its fresh capture
over the deterministic object key while the committed row still carried the first attempt's
`etag` — a row describing bytes the object no longer holds. Phase 1's probe short-circuits that
retry to the existing row before anything is written.

Phase 1 narrows that window; it does not close it. Two attempts of one job can run *concurrently*
— `jobs/worker.py` rejects a heartbeat interval that "risks mid-job reclaim and double-run",
naming the double-run it cannot rule out — and both can pass phase 1 before either writes.
Whichever PUT lands last leaves the other's committed row describing bytes that are gone. So when
an attempt wrote and then found a peer's row, it **repairs** that row, in
`kdive.artifacts.etag_repair.reconcile_row_etag`.

The repair stats the object and writes the etag it *observes*. It must not write the etag this
attempt wrote, which is the obvious form and is wrong: which PUT landed last and which attempt
reaches its locked phase last are independent orderings, so an attempt that wrote first and
locked last would stamp its stale etag over a row another attempt had just set correctly —
introducing the drift the repair exists to remove. Two concurrent attempts are enough to produce
that. Writing an observed value buys a bounded guarantee: every value the repair writes was true
of the object when it was read. It does not guarantee the row ends up correct — see Consequences
— but it excludes setting a row to an etag no version of the object ever carried, which is
exactly what assuming the caller's own etag does.

The repair runs **after** the locked phase, for two reasons. A stat is object-store I/O, which
this ADR keeps out of a locked span. And `diagnostic_sysrq`'s phase 3 can end by raising
`system_changed_state`; psycopg discards a transaction on any exception leaving its block, so a
repair written inside that block would be rolled back on exactly the path that needed it.

When phase 3 refuses to register — the job was canceled, or the System left its live state while
the object was in flight — the handler deletes the objects this attempt wrote, through
`kdive.artifacts.discard.discard_unregistered_objects`, **after** the lock is released.

Because the lock is released by then, the row probe phase 3 made is stale, and a delete decided
on a stale probe is worse than the orphan it prevents: it would leave a committed row pointing at
nothing, which a row-driven reclaim never detects. Each delete is therefore fenced twice,
immediately before it: the object must still carry the etag *this* attempt wrote, and the row
must still be absent (re-probed outside the lock). The row probe is the authoritative fence, so
it runs second — nothing but the delete call itself sits between it and the delete. The delete is
best-effort and logs at warning on failure, because the caller is already on an abort path whose
own outcome is the result that matters.

`console_rotate` keeps its sidecar GET under the lock **deliberately**. It is one small bounded
read and it is the rotation cursor; moving it out would widen the window in which a peer rotation
reads the same cursor and re-derives the same `(gen, index)` parts. This ADR trades the unbounded
PUT loop for a bounded GET rather than moving both.

## Consequences

- The `SYSTEM` and `RUN` advisory locks are held only across database work in these handlers. A
  store outage now fails a job instead of stalling every other operation on that System or Run.
- `console_rotate`'s teardown-race guard is evaluated **twice** — once when the rotation is
  planned, once when its rows are registered. A teardown landing between them fails the second
  evaluation, the parts already written are deleted, and the sidecar cursor is not advanced, so
  the next rotation re-derives the same parts rather than skipping them.
- Each handler's guard semantics are unchanged from the caller's view: a canceled
  `capture_traffic` still returns `None` and writes no row; a `diagnostic_sysrq` whose System left
  `READY` still raises `system_changed_state`; a `console_rotate` on a non-live System still seals
  nothing.
- There is a window, absent before, in which an object exists with no row: between the PUT and
  phase 3's commit. It closes on every path — the row commits, or the object is deleted — except
  a worker crash inside it, or a store fault on the compensating delete. On the cancel and
  changed-state paths the orphan is a deterministic key a later attempt overwrites. **On the
  teardown path it is permanent**: teardown is terminal, so no later attempt writes that key, and
  the reclaim that would have swept it is row-driven and has already run. `console_rotate` leaks
  one object per part it had in flight, not one in total. The warning log names each key because
  it is the only record that will ever exist.
- The etag repair is fenced but not atomic either, and it can still leave a row stale. Its stat
  and its update are separate round-trips, so a PUT landing between them leaves the row
  describing the previous bytes; and two concurrent repairs can commit out of order — stat A
  observes X, a PUT makes the object Y, stat B observes Y, B's update commits, then A's commits
  and puts the row back to X. The repair therefore narrows the drift rather than eliminating it.
  It is worth having because the alternative is not "no drift" but "drift plus a row that names
  bytes the object never held at all", and because nothing in the tree detects either.
- The compensating delete is fenced but not atomic. Its two fences leave a window of one store
  round-trip in which a peer can commit a row, and two attempts that write byte-identical content
  are indistinguishable by etag. Whether that window is reachable at all is a per-site property:
  `capture_traffic`'s guard is `JobState.CANCELED`, which the transition table gives no
  successors, so a refusal there is final; `diagnostic_sysrq`'s guard is `SystemState.READY`,
  which PAUSED and RESTORING return to (ADR-0378), so its refusal is not; `console_rotate`'s rests
  on teardown writing `TORN_DOWN` under this same lock *before* its reclaim runs. Each site states
  its own argument at the call site rather than deferring to the helper.
- Console part objects pin `gzip.compress(..., mtime=0)`. A wall-clock stamp would give one
  `(gen, index)` a new etag on every rotation, which is the identity insert-if-absent is keyed on.
  CPython's default is already 0 from 3.13 on and this project pins 3.14, so the argument is a
  statement of the invariant rather than a fix; the test asserts it on the stored object, so it
  holds whether the call or the runtime supplies it.
- A test at each site pins the span by probing `pg_locks` for the handler's own backend during
  the PUT, against a control that pins the probe reports a lock when one is held. Those tests
  must dispatch the handler the way `JobWorker._run_handler` does (`set_autocommit(True)`): on a
  pooled non-autocommit connection the blocks are savepoints inside one implicit transaction and
  the lock outlives all of them regardless of where the PUT sits (ADR-0506).
- Three sites keep a `store.delete` under a lock — `providers/.../rootfs_reclaim.py`,
  `reconciler/cleanup/uploads.py`, `reconciler/cleanup/upload_orphans.py`. They are deletes
  rather than writes and were out of scope for #1725; this record is the precedent for them.

## Considered & rejected

- **Record all three spans as deliberate and change no code.** The option #1725 explicitly
  offers, and the one 0516 took for `images.upload`. It does not survive the asymmetry: 0516's
  span is one PUT under a `PROJECT` lock on the synchronous request path, where the caller is
  already waiting; `console_rotate` is an unbounded loop of PUTs under the `SYSTEM` lock that
  teardown, boot and revert queue behind. Documenting that is documenting a defect.
- **Fix only the two sites #1725 names.** Its acceptance criterion 3 asserts no third site
  exists; `console_rotate` is a third and the worst of them, missed because it reaches
  `put_artifact` indirectly through `_seal_part` → `_put_part`. Shipping two safe handlers and
  one unsafe one under the same lock scope leaves the real instance in place.
- **Add a unique index on `artifacts (owner_kind, owner_id, object_key)` and rely on `ON
  CONFLICT DO NOTHING`.** This is the shape the database would normally enforce, and it would let
  phase 3 drop its probe. It needs a migration, it needs every existing row in `artifacts` to be
  conflict-free under the new key first, and it still would not decide what happens to the object
  when the PUT wins and the insert loses — which is the actual question. ADR-0528 resolves that
  separate decision with a database backstop and explicit adoption of the winning row; it does not
  replace this record's lock-span and compensation decision.
- **Skip the compensating delete and let the object leak.** Cheapest, and defensible under a
  prefix-driven sweep. It is not defensible here: reclaim reads the rows, so the leak is
  permanent rather than deferred, and for `capture_traffic` the leaked object is `SENSITIVE`.
- **Delete on the strength of phase 3's own row probe.** The obvious reading, and a data-loss
  bug: that probe was taken under a lock this code has since released, so between it and the
  delete a peer attempt can register the key and the delete then strands a committed row. The
  fences exist because the probe and the delete are no longer in the same critical section.
- **Repair the row to the etag this attempt wrote.** The form `boot_evidence.py` and
  `remote_libvirt/console/snapshot.py` use, and sound there because each does its probe and its
  write inside one critical section. Here the PUT is outside the lock, so an attempt can write
  first and lock last; assuming its own etag then overwrites a correct row with a stale value.
  The repair stats the object instead.
- **Drop the repair and let a consumer detect the drift.** There is no such consumer.
  `vmcore.py`'s etag check compares a stat against the `StoredArtifact` *it just wrote*, as a
  pre-commit guard on its own capture; it never reads a committed `artifacts.etag`, and the
  artifact read path conditions its GET on a freshly-stat'd etag rather than the row's. Dropping
  the repair would leave the drift in place with nothing to notice it.
- **Delete the object from inside phase 3's locked block.** Simpler control flow, and wrong for
  the reason this whole record exists — it puts object-store I/O back under the lock.
- **Move `console_rotate`'s sidecar GET out of the lock too, for symmetry.** The cursor read is
  what decides which parts exist; unlocking it widens a peer-rotation window for a bounded
  single small read. The unbounded write loop was the defect.
