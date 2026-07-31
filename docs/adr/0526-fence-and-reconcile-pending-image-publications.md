# 0526 — Fence and reconcile pending image publications

## Status

Accepted

## Context

Image publication commits a `pending` catalog row before writing its object. A worker death after
the PUT but before registration leaves a row that the current dangling-image repair never reclaims,
because it treats any present object as healthy. The row is unresolved and, for a private image,
continues to reserve count and byte quota.

The same deadline can race a live slow publisher. Once `pending_since` ages past
`KDIVE_IMAGE_PUBLISH_GRACE_SECONDS`, the repair may remove the row while a large object is still
being written. Extending or heartbeating that deadline would create a second liveness lease beside
the worker lease, with its own renewal and failure semantics.

Recovery can register an abandoned object only when the object still matches the row that reserved
it. The row already persists the expected key, byte size, and SHA-256 digest, but image PUTs do not
currently ask the object store to retain the SHA-256 for a later HEAD comparison.

## Decision

Each reserved image row defines a publication-fence name from its UUID. Before the first object
write, the publisher takes the corresponding Postgres session advisory lock. It revalidates the
reservation after acquiring the lock, holds the lock through the PUT, HEAD gate, registration, and
private-image registration audit, then releases it. The lock is image-scoped; the PROJECT advisory
lock still ends when quota reservation commits. PostgreSQL releases the session lock if the
publisher connection or process dies.

The dangling-image repair considers an expired row one at a time. In a short transaction it tries
the same advisory key as a transaction lock. Contention means an active publisher owns the row, so
the repair skips it without waiting. A granted lock orders recovery before any publisher that has
not started its write; the repair re-reads and row-locks the candidate before touching the store.

For an expired `pending` row:

- a missing object causes the row to be deleted;
- a present object whose HEAD size and SHA-256 equal the persisted `size_bytes` and `digest` is
  registered;
- a present object with missing or mismatched integrity evidence is deleted, its absence is
  confirmed with another HEAD, and only then is the row deleted.

Every image qcow2 PUT supplies its base64 SHA-256 through `ChecksumSHA256`, so later HEAD requests
can make the integrity decision without downloading a potentially multi-gigabyte image. Objects
written before this decision with no stored checksum are unverifiable and follow the invalid-object
path. Registered rows retain the existing behavior: a missing object is removed after the deadline,
while a present object is not revalidated by this repair.

Store errors abort the candidate transaction and preserve the row for a later pass. If deletion
returns but the follow-up HEAD still sees the object, the row is also preserved. A crash after
object deletion but before row deletion leaves a pending row with a missing object, which the next
pass removes. A crash after the publisher PUT but before registration releases the advisory lock
and leaves a valid object that the next pass registers.

## Consequences

- A genuinely active publisher is protected for the duration of its write without sizing or
  renewing another deadline.
- The fence holds one database connection and one session advisory lock across a potentially long
  PUT, but no database transaction and no project-wide lock.
- Recovery registers only byte-identical objects and releases abandoned private-image quota after
  invalid or absent objects are reclaimed.
- Recovery still relies on the object store's existing HEAD-after-delete visibility contract. It
  does not claim deletion complete while HEAD still observes the key, and the rowless leaked-image
  sweep remains a backstop after a committed row removal.
- The existing same-identity writer ordering problem is not broadened here. This fence orders a
  publisher against the reconciler; it does not redefine the catalog uniqueness contract for two
  independent publications.

## Considered & rejected

**Persist a publisher heartbeat and lease expiry.** This creates a second deadline whose renewal
must remain live while a blocking SDK call runs. A transient database failure could let the lease
expire while the PUT is still active, reopening the destructive race the fence exists to close.

**Hold a row lock and database transaction across the PUT.** This gives the necessary ordering but
keeps a long-lived transaction snapshot during a transfer and can delay vacuum cleanup globally.
A session advisory lock has the same backend-death release property without the long transaction.

**Trust object presence and register every expired pending row.** Presence proves neither identity
nor completeness. Registering a partial or overwritten object would make the catalog claim bytes
that later materialization rejects.

**Always delete expired pending rows and let the leaked-object sweep clean up.** This discards a
complete object after the only missing step was a catalog flip and adds a rowless-object window.
