# ADR 0502 — A job-held write lease, taken under the owner lock the sweep also takes, closes the orphan sweep's delete race

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** KDIVE maintainers

## Context

[ADR-0455](0455-upload-prefix-orphan-sweep.md) §3 disclosed a residual and
[ADR-0497](0497-finalize-verifies-its-object-before-committing-rows.md) §3 confirmed it survives:
"This mitigates the race; it does not close it … **the sweep still deletes the object**". ADR-0497's
fence is at the *other* end — `finalize_capture` heads each object before committing `artifacts`
rows — so a Run no longer records rows against destroyed bytes. The bytes are still destroyed.
ADR-0497's own Considered & rejected names the option that would close it and records it as unowned;
#1687 owns it.

The window is precise. `_delete_if_still_reclaimable`
(`reconciler/cleanup/upload_orphans.py`) re-heads the key, re-runs `reclaimable_upload_keys`, and
then calls `store.delete` **holding nothing**. Every fence it evaluates is a *committed* row or a
store mtime read before the decision, so a write that lands after the decision and before the
delete is invisible to it. The reachable writer is local-libvirt's server-side `put_stream` under
`local/runs/`: `precheck_run` deliberately releases the `LockScope.RUN` lock before the capture
([ADR-0244](0244-per-run-vmcore-capture.md)) so a multi-GiB stream is not held under it, the vmcore
lane mints no upload window, and the sweep takes no Run lock at all. The remote-libvirt guest PUT is
not a path in: it writes under `remote-libvirt/`, outside every swept root.

A conditional delete is not available. ADR-0497 §1 measured `If-Match` on `DeleteObject` against both
pinned MinIO releases: the header reaches the wire, the call returns success, and the object is
destroyed on every arm. `ObjectStore.delete` therefore keeps its unconditional shape.

### Why the issue's proposed fix is rejected

#1687 proposes minting an `upload_manifests` row for the `(runs, run_id)` owner before the capture's
write, on the grounds that the sweep's classify already treats such a row as a reason to skip. The
classify does — and the fix is still wrong, for three independent reasons, each verified in the tree:

1. **`upload_manifests` is the *reaper's* candidate table, and the reaper is the more dangerous
   deleter.** `repair_abandoned_uploads` (`reconciler/cleanup/uploads.py`) selects candidates on
   `deadline < now()` alone; `_claim_abandoned_prefix` dooms every key under the **owner** prefix
   holding no committed `artifacts` row; and `_sweep_uncommitted_objects` then deletes that list
   **unconditionally — no re-read, no grace, no lock**. Its own docstring says "Anything that
   lengthens this phase widens #1557", and #1557 is open. Minting a manifest row to hide from the
   orphan sweep hands the in-flight object to a deleter with strictly fewer fences than the one it
   was hiding from, and does so on a timer the capture does not control.
2. **Primary-key collision with a real upload window.** `replace_manifest` is a full-row upsert on
   PK `(owner_kind, owner_id)` (`artifacts/upload_manifest.py`), and `runs` is **already** a live
   upload owner — `artifacts.create_run_upload` mints one, gated on `RunState.CREATED`
   (`mcp/tools/catalog/artifacts/uploads.py`). A capture fence and a build-upload window would be
   the *same row*: the fence clobbers the window's `manifest` and `deadline`, and either finalize's
   `delete_manifest` destroys the other's.
3. **The row would be a lie.** `manifest jsonb` holds the declared `(name, sha256, size_bytes)` set
   `complete_build` compares stored objects against (`db/schema/0006_upload_manifests.sql`). A
   capture knows none of those before it writes. A fence shaped like a contract it cannot satisfy is
   a fence that will be read as one.

### Why a fence alone is not a closure

A third `NOT EXISTS` term is necessary and **not sufficient**. The sweep's classify reads committed
rows at some instant T; a writer that commits its fence after T and PUTs before the delete is still
destroyed. Mint-before-write and classify-before-delete have to be *totally ordered*, not merely
both present. Nothing in the current code orders them, because the sweep holds no lock.

## Decision

Add a **write lease**: a purpose-scoped row a job holds over an upload owner's prefix while it
writes objects there, minted under the owner's advisory lock — the same lock the sweep now takes
before it deletes.

1. **New table `object_write_leases`** (migration `0084`), PK `(owner_kind, owner_id, job_id)`,
   `job_id` a `jobs(id)` FK `ON DELETE CASCADE`. It is *not* `upload_manifests`: separate table,
   separate key, no `manifest`, and no reaper reads it. The PK includes the holder, so two
   concurrent captures of one Run each hold their own lease and neither drops the other's — the
   collision reason 2 rejects cannot recur here.
2. **A third fence in `_RECLAIMABLE_SQL`:** a key is reclaimable only if its owner holds no lease
   *whose holding job is still live* — `jobs.state = 'running' AND jobs.lease_expires_at > now()`.
   The liveness term is what makes the lease self-limiting: the deadline governing it is the job
   lease the worker already heartbeats (`jobs/queue.py`), so it cannot lapse under a healthy writer
   however long the capture streams, and a lease whose holder died fences nothing from the next pass
   onward. Every term is still decided in Postgres `now()`.
3. **The sweep takes the owner's lock before it deletes.** `_delete_if_still_reclaimable` now runs
   its re-classify and its `store.delete` inside one transaction holding
   `pg_try_advisory_xact_lock` for `(RUN|INVESTIGATION, owner_id)`. The mint takes the same lock, so
   a mint either precedes the classify (which then sees it and skips the key) or blocks until the
   delete has already happened (so its write follows the delete and is not destroyed). That is the
   ordering, and it is the closure.
4. **`try`, not a blocking acquire.** A contended lock means someone is actively working that owner,
   so the sweep skips the key — it is neither deleted nor counted as a fault, and the next pass
   re-derives it. A blocking acquire would let one long lock holder stall a reconciler pass that has
   no deadline, behind allocation expiry and orphaned-System repair; skip-and-re-derive is the same
   trade ADR-0455 §5/§6 already make for every other per-key fault. `try_advisory_xact_lock` is
   added to `db/locks.py` beside the blocking helper and checks the transaction status the same way
   — after the acquire, because a `true` returned outside a transaction is a lock already released.
5. **The mint is `capture_handler`'s, between `precheck_run` and `retriever.capture`;** the release
   is inside `finalize_capture`'s existing transaction, which already holds `LockScope.RUN`. Mint
   before the write and release after the rows commit is the whole ordering requirement.
6. **A reap for hygiene, not for correctness:** `reap_stale_write_leases` deletes every lease with no
   live holder. `capture_handler`'s `except Exception:` does not release the lease and must not — a
   worker killed mid-write releases nothing at all — so the row outlives its writer and something
   has to collect it. Because the fence is liveness-checked, a lease the reap has not yet collected
   already protects nothing; the reap bounds table growth rather than bounding exposure.
7. **`lock_scope_for`, `_LOCK_SCOPES` and `UPLOAD_OWNER_KINDS` move to
   `artifacts/upload_manifest.py`,** beside `UPLOAD_TENANT` and `UploadOwnerKind`. The mint and the
   sweep's delete must lock the *same* scope for the ordering above to hold, and the mint lives in
   the artifacts layer while the sweep lives in the reconciler; a second copy of a two-entry map
   whose drift silently unpairs the lock is not an option. No shim is left behind.

## Consequences

- **The race is closed for every writer that takes a lease, and the capture lane now takes one.** A
  fence can only protect a writer that declares itself, so the raw-store residual is unchanged for a
  writer that mints nothing: `test_a_put_inside_the_same_key_s_re_read_delete_gap_is_destroyed` still
  passes, and now pins exactly that — the unleased path — rather than the capture path. It is kept,
  not deleted, because it is the regression test for the sweep's behaviour toward an undeclared
  writer.
- **`control.capture_traffic`'s pcap is such an undeclared writer and is deliberately not covered
  here.** It writes under the same Run prefix but holds `LockScope.RUN` across its whole
  `put_artifact`, so the sweep's `try` lock now *fails* against it and the key is skipped — it gains
  protection from item 3 without a lease. That is a weaker guarantee than a lease (it holds only
  while the lock is held) and it is not what this ADR claims; the lease is the mechanism for a writer
  that cannot hold a lock across its write.
- **The sweep now holds an advisory lock and a snapshot across one `delete_object`.** Per key, that
  is one lock acquire, one query and one round trip — strictly cheaper than
  `_claim_abandoned_prefix`, which already holds `LockScope.RUN` across a whole paginating
  `list_prefix`. It does mean `reclaimable_upload_keys`' "no snapshot across blocking store calls"
  property no longer holds on the per-key path; it still holds on the page classify.
- **A capture whose job lease lapses mid-write loses its fence.** The exposure window is exactly the
  window in which the queue has already declared the job abandoned and `repair_abandoned_jobs` will
  reclaim it, and ADR-0497's verify still refuses to commit rows against the lost bytes. Tying the
  fence to a *second*, independent deadline was rejected for the opposite failure: a value shorter
  than the longest capture silently reopens this race, and nothing in the tree bounds a capture's
  duration.
- **`jobs` becomes load-bearing for object-store safety for the first time.** A lease's protection is
  only as good as `jobs.state`/`jobs.lease_expires_at`. That is deliberate — it is the one liveness
  signal in the tree that a live writer already renews — but it does couple the sweep to the queue's
  state machine, and a future change to how a running job's lease is represented has to consider this
  fence.
- **No new configuration.** The lease introduces no knob, because its deadline is the job lease's.
- Schema: one additive, forward-only migration (`0084`). No config, dependency, MCP tool-surface,
  RBAC or AI-surface change.

## Considered & rejected

- **An `upload_manifests` row minted before the write** — the issue's proposal and ADR-0497's named
  option. Rejected for the three reasons in Context: it is the reaper's candidate table and widens
  #1557, it collides on PK with `artifacts.create_run_upload`'s real window for the same Run, and its
  `manifest` column cannot be honestly populated by a capture. Only the *classify* half of the issue's
  reasoning survives, and it survives as item 2 above against a different table.
- **A conditional `DeleteObject`** — measured inert on both pinned MinIO releases (ADR-0497 §1) and
  not re-litigated here.
- **Holding `LockScope.RUN` across the capture** — reverses ADR-0244 for a multi-GiB stream, and
  cannot cover `local/investigations/`, whose objects arrive as presigned client PUTs the server
  never spans. ADR-0497 already rejected it on those grounds; the lease is what lets the writer
  declare itself *without* holding a lock across its write.
- **A lease with its own `deadline` column, reaped on `deadline < now()`** — the literal ADR-0444
  pattern. Rejected because it needs a value larger than the longest capture and nothing bounds a
  capture's duration: too small silently reopens this exact race (the worst failure a fence can
  have), too large only delays a leak drain. The job lease is the same deadline pattern against a
  clock a live writer already advances.
- **Fixing #1557 first, by wiring `reclaimable_upload_keys` into `_sweep_uncommitted_objects`** —
  costed in `upload_orphans.py` as "a call rather than a rewrite" and worth doing, but it makes this
  issue depend on an open one and still leaves reasons 2 and 3 standing against the manifest row. Out
  of scope.
- **A blocking `advisory_xact_lock` in the sweep** — see Decision item 4.
- **Leaving the reap out, since the fence is liveness-checked** — correct for exposure, wrong for
  growth: nothing else would ever delete a dead capture's row.
