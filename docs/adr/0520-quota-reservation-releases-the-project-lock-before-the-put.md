# 0520 — The private-upload quota reserves on the row and releases the PROJECT lock before the PUT

## Status

Accepted (2026-07-30)

Partially supersedes
[ADR-0516](0516-private-upload-holds-the-project-lock-across-its-put-by-design.md) §2 — the span
that record accepted *pending #1726* is now shortened. Everything else in ADR-0516 stands: the
`require_top_level_transaction` assertion, the classification of the presign and
complete-rootfs-upload sites, and the finding that the transaction was already top-level.

## Context

`_publish_under_quota` (`services/images/upload.py`) held `LockScope.PROJECT` across two unbounded
operations:

1. `_project_usage` issued **one object-store HEAD per live private row** in the project — up to
   `KDIVE_IMAGE_PRIVATE_MAX_COUNT` (default 50) network round trips — to sum the project's bytes.
2. `publish_image` then wrote the qcow2, a PUT bounded only by `KDIVE_IMAGE_PRIVATE_MAX_BYTES`
   (default 50 GiB).

So one project's uploads serialized behind each other for the full duration of one upload's HEADs
plus its PUT. ADR-0516 recorded the span as deliberate rather than accidental and pinned the
transaction top-level so it could not silently extend *past* the PUT to the caller's commit. It
explicitly left the duration open as #1726, and named the two shapes that could close it: reserve
the bytes in a row first and publish outside the lock, or admit optimistically and reconcile.

The lock was load-bearing. It is what made the quota check atomic: `_project_usage` read the total
and `_quota_denial` decided against it, and if the lock were simply dropped, two concurrent uploads
would both read the pre-write total and both pass a cap they jointly breach.
`test_concurrent_uploads_cannot_both_pass_the_cap` pins that outcome.

Two facts about the existing design make the reservation shape cheap rather than novel:

- **The `pending` row is already a reservation.** ADR-0092's publish is row-first: the catalog row
  is written *before* the object, precisely so a rowless object cannot exist. `_LIVE_PRIVATE_STATES`
  already counts `pending` rows toward the quota. The only thing missing was the row's *size* — a
  `pending` row's object does not exist yet, so the HEAD returned `None` and it contributed 0 bytes.
- **The reclaim path for an abandoned `pending` row already exists.**
  `reconciler.cleanup.images.repair_dangling_images` removes a non-`defined` row whose object HEAD
  is missing past `pending_since + grace` (`KDIVE_IMAGE_PUBLISH_GRACE_SECONDS`, default 3600s),
  under a delete fenced on the same deadline so a re-armed publish is never wedged.

## Decision

### 1. `image_catalog.size_bytes` carries the object's size, written at reservation time

Migration `0089` adds `size_bytes bigint NOT NULL DEFAULT 0` with a `>= 0` CHECK. The publish path
writes the exact byte length of the object it is about to write — known before the write in both
callers (the upload path has already buffered the bytes to compute the digest; the build path stats
the built qcow2). It is not a *declared* size a client could inflate or under-state: no caller
supplies it, and the same value is what the PUT writes.

A `defined` baseline has no object, so 0 is the truthful value there, not a placeholder — which is
why the column is `NOT NULL DEFAULT 0` with no CHECK tying it to `object_key`.

### 2. The quota read is one SQL aggregate, not N store HEADs

`_project_usage` becomes `SELECT count(*), COALESCE(SUM(size_bytes), 0)` over the project's live
private rows. Zero object-store round trips. This is a strict improvement independent of the lock
span: it is the "stored aggregate" half of the issue, and it removes the N-in-artifacts scaling
term entirely.

The read is *not* maintained as a separate counter row or table. Summing a column of at most
`KDIVE_IMAGE_PRIVATE_MAX_COUNT` rows per project inside the lock is already cheap, and a
denormalized counter would be a second source of truth that can drift from the rows the sweeps
delete.

The aggregate **excludes the in-flight `pending` row of the identity this reservation is about to
publish**, because `reserve_publish` will *adopt* that row and overwrite its `size_bytes` rather
than add a second one. Counting it would charge the project twice for one image, and the case that
makes concrete is the retry §4 names as the recovery: a user whose write failed, retrying the same
image, would be denied by the bytes their own abandoned reservation is holding — for the whole
publish grace. Only `pending` is excluded; a `registered` row of the same identity is not adoptable
and does still occupy quota. At most one such row can exist, because every private reservation for
one identity serializes on the PROJECT lock and adopts rather than inserts.

### 3. The lock spans the reservation only; the PUT runs unlocked

`publish_image` is split into three composable steps, and the upload path interleaves the lock
between the first and the second:

- `reserve_publish(conn, request, *, size_bytes)` — the row-first adopt/insert, now recording
  `size_bytes`. Returns a `PublishReservation`.
- `write_publish_object(store, reservation, source)` — digest verify, qcow2 PUT, HEAD gate, and the
  best-effort config sibling. No database access at all.
- `finish_publish(conn, reservation, *, config_written)` — the flip to `registered`.

`publish_image` composes all three unchanged for the build path. `_publish_under_quota` becomes:

```
require_top_level_transaction(conn, ...)
async with conn.transaction(), advisory_xact_lock(conn, LockScope.PROJECT, project):
    count, used_bytes = await _project_usage(conn, project)      # one aggregate query
    denial = _quota_denial(...)
    if denial is None:
        reservation = await reserve_publish(conn, request, size_bytes=new_bytes)
# lock released, transaction committed — the reservation is visible to every other upload
... audit and raise if denied ...
await write_publish_object(store, reservation, source)           # the multi-GiB PUT, unlocked
async with conn.transaction():
    entry = await finish_publish(conn, reservation, config_written=...)
    await _audit_registration(conn, entry, principal=principal)
```

The locked section is now bounded: one aggregate `SELECT` and one `INSERT`/`UPDATE` of a single row.
It contains no object-store call and no unbounded loop.

The fail-closed property is preserved by *committing the claim* rather than by holding the lock over
the work: the reservation row commits inside the lock, so the next upload's `SUM(size_bytes)` sees
it. Two concurrent uploads cannot both pass the cap because the second one's aggregate read happens
after the first one's reservation is durable.

Reserved size equals actual size, so there is no reconcile step. The reserve-then-**reconcile**
shape the issue offered is only needed when the size is a client-declared estimate; here it is the
length of bytes already in hand.

`require_top_level_transaction` stays, and its justification strengthens: the whole point is that
the `conn.transaction()` really commits the reservation and really releases the lock before the PUT.
Under a savepoint neither happens, and the shortened span would silently become the old one.

### 4. An abandoned reservation is reclaimed by the existing dangling-image sweep

This is the cost of the change, and it is paid to an existing mechanism rather than a new one.

Before: a PUT failure rolled the whole locked transaction back, so no row survived. After: the
reservation is committed, so a failed PUT — or a worker killed mid-upload, or the process dying
between reserve and finish — leaves a `pending` row holding its `size_bytes` against the project's
quota.

That row is reclaimed by `repair_dangling_images`: its object HEAD is missing and its
`pending_since + KDIVE_IMAGE_PUBLISH_GRACE_SECONDS` deadline elapses, so the reconciler deletes
it and the quota is released. The bound on how long such a reservation consumes quota is
therefore `KDIVE_IMAGE_PUBLISH_GRACE_SECONDS` (default one hour), not forever. No new reclaimer,
no new deadline column, no lease table.

**That covers the object-absent case only, which is not every case.** Two windows the sweep does
not close, both disclosed rather than fixed here:

- **The write landed but the flip did not.** If the process dies between `write_publish_object`
  returning and `finish_publish` committing, the row is `pending` with its object **present**.
  `repair_dangling_images` skips it (its `head_present` check passes) and `repair_leaked_images`
  skips the object (a row references it), so nothing reaps it and it holds its bytes and its
  count slot indefinitely. A re-publish of the same identity adopts it, which is the only
  recovery. The window is one round trip wide, but it is exactly the width a worker restart or a
  deploy lands in. This is a pre-existing property of the row-first publish — the build path has
  had it since ADR-0092 — but this change is what brings the private-upload path into it, and the
  quota is what gives it a lasting cost. Filed as #1757 rather than fixed here: closing it
  means teaching the reconciler to resolve a pending-with-object row, which is a reconciler
  decision and interacts with the next bullet.
- **A publish slower than the grace is swept mid-upload.** `pending_since` is stamped at
  reservation and the PUT now runs after the commit that makes the row visible, so a transfer
  lasting longer than `KDIVE_IMAGE_PUBLISH_GRACE_SECONDS` can have its live reservation deleted
  underneath it; `finish_publish` then raises `RuntimeError("image_catalog row … vanished before
  registration")`. A 50 GiB image is in-bounds by `KDIVE_IMAGE_PRIVATE_MAX_BYTES`, so at ~10 MB/s
  this is reachable at the default one-hour grace. Operators whose uploads are that large must set
  the grace above their slowest expected transfer. Re-arming `pending_since` mid-write would need
  a heartbeat, which is the second-deadline trap ADR-0502 rejected for `object_write_leases`.
  Tracked with the previous bullet in #1757, since a sweep taught to resolve a pending-with-object
  row must not race a slow publish about to flip it — one fence, one design.

**No bespoke rollback is added on the PUT-failure path.** ADR-0092 already states that the recovery
path for a crash mid-publish is the reconciler, not a rollback, and a rollback would only cover the
raised-exception case while doing nothing for the killed-worker case that needs it most — leaving
two mechanisms where the sweep must be correct anyway. The upload path now behaves exactly like the
build path, which has committed its `pending` row before the PUT since ADR-0092.

### 5. The adopt refreshes `digest` alongside `size_bytes`

Found while testing the retry path, and fixed here because the reservation is what makes it
reachable often enough to matter: `_adopt_or_insert_pending` refreshed `object_key`,
`kernel_config_key` and `pending_since` but never `digest`. An adopted row therefore kept the
abandoned attempt's digest while the retry wrote different bytes, registering an image whose
object can never satisfy the materialization fetch's `sha256(object) == row.digest` gate — the
permanent unfetchability `_verify_source_digest` exists to prevent, arrived at from the other
side. The adopt now assigns `request.digest`.

This also applies to a `defined` baseline, including a `config`-managed one that
`inventory/reconcile/images.py` seeded with an operator-declared digest: realizing it as a
published image replaces that digest with the published object's. That is the correct direction —
a row with an object must describe *that* object — but it is a behaviour change beyond the retry
case, so it is recorded rather than left to be discovered.

### 6. ADR-0516 is annotated by appending, not by striking

`docs/adr/README.md` describes marking a partially superseded section with strike-through
(`~~…~~`). The records gate does not permit that: `check-records.sh` raises `E-REWRITE` when a
merged record's `## Decision` or `## Consequences` **drops** a line the base ref had, and wrapping
prose in `~~` rewrites every line it touches. The gate is the authority, so ADR-0516 keeps its
prose byte-for-byte and gains only appended text: a partial-supersession banner beneath its
`## Status`, an italic *Superseded by 0520* note after §2, and one after the matching Consequences
bullet. That satisfies the README's intent — a reader of the superseded prose is told, in place,
that it no longer holds — without an edit the gate rejects.

### 7. The registration flip is fenced on the reservation, not on the row id

Releasing the lock before the write opens a window §3 did not close on its own. Two uploads of
the *same* `(provider, name, arch)` to one project now interleave: the first reserves, and the
second — which blocked on the PROJECT lock, so its usage read is correct — **adopts that same
`pending` row** under `_adopt_or_insert_pending`'s `FOR UPDATE`, because ADR-0092 idempotency
deliberately adopts an in-flight row of the identity rather than colliding with it. The adopt
overwrites `digest` and `size_bytes` with the second attempt's. Both attempts then leave the lock
and PUT the same deterministic object key.

`_registered` used to flip on `id` alone. That let **both** attempts flip the one row to
`registered` and return an entry, so both callers were told they succeeded, and at most one of
their digests was still on the row — the other had published a live, quota-consuming image whose
object can never satisfy the materialization fetch's `sha256(object) == row.digest` gate.

This was not reachable before this change: the PROJECT lock spanned the whole publish, so the
second attempt could not observe an in-flight `pending` row at all. It is a consequence of the
shorter span and is fixed here rather than deferred.

The flip is now fenced on the reservation's own identity —
`WHERE id = %s AND digest = %s AND object_key = %s` — and a zero-row update raises
`ErrorCategory.CONFLICT`. Exactly one attempt registers; the superseded one is told so in a typed
error. This is the concrete state-conflict seam `CONFLICT` was reserved for and previously had no
emitter (`domain/errors.py`). The same fence turns §4's swept-mid-write case from a bare
`RuntimeError("image_catalog row … vanished")` into that typed error.

**Residual, stated rather than claimed closed.** The fence guarantees one *registration*; it does
not order the two PUTs. Both attempts write the same key unlocked, so if the superseded attempt's
PUT lands *after* the winner's, the object holds the loser's bytes while the row carries the
winner's digest — an unfetchable image again, now with one caller correctly told it failed. Fully
closing this needs same-identity publishes to serialize across the write, which is the object-key
collision tracked in #1756 (that issue's `registered`-name case and this one share the cause: one
deterministic key, no writer exclusion). The fence is the part that belongs in this change; the
exclusion is a design that issue owns.

## Consequences

- Concurrent uploads to one project no longer serialize behind each other's HEADs and PUT. They
  serialize behind a single-row write. Uploads to different projects were already unaffected.
- The quota read costs one query instead of up to 50 object-store round trips, on every upload,
  whether or not it is contended.
- A project's quota can be held by an orphaned reservation for up to
  `KDIVE_IMAGE_PUBLISH_GRACE_SECONDS` after a failed or killed upload. Operators who shorten that
  setting shorten this window with it; the setting's other consumers (`repair_leaked_images`,
  `repair_dangling_images`) already tie it to publish liveness, so the meanings agree.
- A quota denial no longer needs the PUT to have been attempted, and an over-cap upload still writes
  nothing: the denial branch reserves no row, rolls back the locked transaction, and audits on a
  fresh one exactly as before.
- `ImageCatalogEntry` gains `size_bytes: int = 0`. The model is `extra="forbid"` and the publish
  path reads rows back with `SELECT *`, so the field is required, not optional. It is a size in
  bytes of an object the requesting project already owns — no new information is disclosed to any
  principal that could not already HEAD the object.
- Rows that predate migration `0089` carry `size_bytes = 0` and so contribute nothing to their
  project's bytes total until they expire. This is a greenfield rewrite with no deployment whose
  private-image quota is load-bearing (`AGENTS.md`), and private rows carry a
  `KDIVE_IMAGE_PRIVATE_LIFETIME_MAX_SECONDS`-bounded `expires_at`, so the window is bounded and
  closes itself. The **count** cap is exact from the first upload, since it never depended on size.
- `publish_image` keeps its signature and behaviour; `image_build.py` is untouched. The three new
  functions are the seam the lock needs, not a second publish path — `publish_image` is their only
  other composition.
- `ErrorCategory.CONFLICT` gains its first emitter (§7). It was defined-but-unemitted, reserved in
  `domain/errors.py` for "a uniqueness/state conflict" pending a concrete seam; this is that seam.
  Agents calling `images.upload` can now receive it, and the correct response is to retry — the
  retry adopts the winning row rather than colliding with it (§2's exclusion is what keeps that
  retry from being quota-denied).
- The build path inherits the same fence. `publish_image` composes `finish_publish`, so two
  concurrent builds of one public identity now also get one registration and one `CONFLICT`
  instead of two claimed successes. That path had the race before this change; it is fixed by
  being on the shared step rather than by a separate change.

## Considered & rejected

- **Accept the span and document it.** The issue's other acceptance criterion, and what ADR-0516
  did as an interim. Rejected: the repo owner chose to shorten. The scaling terms are real (N
  artifacts × store latency, plus image size ÷ bandwidth) and the shorter span costs one column.
- **A denormalized per-project usage counter row or table.** The literal "stored aggregate" shape.
  Rejected: it is a second source of truth that every row-deleting path — `repair_dangling_images`,
  `repair_expired_private_images`, `expire_one_private_image`, the inventory prune — would have to
  decrement, and a missed decrement leaks quota permanently with no sweep to heal it. Summing a
  column bounded by the count cap is cheap enough that the drift risk buys nothing.
- **A dedicated `image_quota_reservations` table, modelled on `object_write_leases` (ADR-0502) or
  `rootfs_fetch_leases` (ADR-0515).** Read first, as the in-tree reservation precedent. Rejected
  because both of those exist to record a claim that *has no row of its own* — a capture streaming
  into an owner's prefix, a fetcher staging a base. This claim already has a row: the `pending`
  catalog row, which is written before the object by design, already counts toward the quota, and
  already has a deadline (`pending_since`) and a sweep. A second table would duplicate all four and
  add a reconciliation between them.
- **Admit optimistically and reconcile after the PUT** (the issue's second shape, and ADR-0516's
  "admit-then-reconcile"). Rejected: it inverts the quota from fail-closed to fail-open for the
  duration of every upload. `test_concurrent_uploads_cannot_both_pass_the_cap` would have to be
  weakened to allow both uploads to commit and one to be clawed back, and clawing back a *completed*
  registered image is user-visible data loss, where declining an upload up front is not.
- **Keep the HEAD-based sum and only shorten the PUT half of the span.** Halves the fix: the
  reservation still has to record a size for a concurrent reader to see it, and once the column
  exists the HEAD loop has no remaining caller. Keeping it would leave N round trips inside the
  lock for no benefit.
- **Backfill `size_bytes` for pre-migration rows by HEADing their objects, or fall back to a HEAD
  for rows where it is 0.** Rejected: a migration cannot reach the object store, and a fallback
  branch is a permanent second read path in the hot query to serve rows that expire on their own.
  The residual is disclosed above instead.
- **Delete the reservation row explicitly when the PUT raises.** Rejected under §4: it covers only
  the raised-exception case, leaves the killed-worker case to the sweep regardless, and so adds a
  second reclaim mechanism that cannot replace the first. ADR-0092 already settled this shape.
- **Take a shorter-lived lock scope (e.g. per-identity) instead of PROJECT.** Rejected: the quota is
  per-project, so the mutual exclusion has to be per-project too. The change needed was the lock's
  *duration*, not its scope, and the scope is already correct.
