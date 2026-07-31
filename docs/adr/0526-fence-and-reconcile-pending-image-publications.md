# 0526 — Fence and reconcile pending image publications

## Status

Accepted

- **Narrowly supersedes:** [ADR-0317](0317-image-kernel-config-offer.md) §2 and
  [ADR-0336](0336-staged-kernel-config-offer.md)'s shared deterministic config-key rule for the
  publish lane only, plus [ADR-0112](0112-systems-inventory-config.md)'s config update/prune
  ownership while a config-managed image row is `pending`. Staged-image config keys remain
  deterministic.

## Context

Image publication commits a `pending` catalog row before writing its object. A worker death after
the PUT but before registration leaves a row that the current dangling-image repair never reclaims,
because it treats any present object as healthy. The row is unresolved and, for a private image,
continues to reserve count and byte quota.

The same deadline can race a live slow publisher. Once `pending_since` ages past
`KDIVE_IMAGE_PUBLISH_GRACE_SECONDS`, the repair may remove the row while a large object is still
being written. Extending or heartbeating that deadline would create a second liveness lease beside
the worker lease, with its own renewal and failure semantics.

Recovery can register an abandoned object only when the object still matches the attempt that
reserved it. The row already persists the expected byte size and SHA-256 digest, but object keys
are deterministic across attempts and image PUTs do not currently ask the object store to retain
the SHA-256 for a later HEAD comparison. A late PUT must not be able to recreate a key after its
reservation was reclaimed.

Private registration also has an atomic audit invariant: the catalog flip and its audit row commit
together under the initiating principal. The pending row does not currently retain that principal.
The optional kernel-config key has a similar invariant: a registered row advertises it only when
the sibling object exists.

## Decision

Migration 0093 adds a non-null publication-attempt UUID to each `pending` row and a nullable
initiating principal. Every reservation, including adoption, mints a fresh attempt UUID. Every
private insert or adoption writes the current authenticated principal in that same reservation
transaction, replacing any prior attempt's actor; every public reservation writes `NULL`. Normal
and recovered registration clear both fields only after using the principal for any atomic audit.

The qcow2 and optional config object keys include the attempt UUID, so a PUT from a superseded,
cancelled, or disconnected attempt can land only at its own now-rowless key; it cannot recreate or
overwrite the key a later attempt or recovery validated. This narrowly replaces ADR-0317/0336's
shared deterministic config key for the publish lane. A new attempt-aware publish-key helper owns
both qcow2 and config keys. Inventory/staged-image capture retains the deterministic
`config_object_key` helper because that lane has no publication attempt.

Each reserved image row defines a publication fence from its row UUID. New session-scoped and
transaction-scoped helpers use one shared `LockScope.IMAGE_PUBLISH` bigint derivation; they do not
use the existing separately salted leadership-lock namespace. Before the first object write, the
publisher takes the session form, revalidates the attempt in a committed short transaction, and
proves the connection is transaction-idle before starting the PUT. It holds the session lock
through the PUT, HEAD gate, registration, and private-image registration audit, then releases it.
The lock is image-scoped; the PROJECT advisory lock still ends when quota reservation commits.

The dangling-image repair considers an expired row one at a time. In a short transaction it tries
the transaction form of that exact advisory key. Contention means a publisher with a live database
session owns the row, so the repair skips it without waiting. A granted lock orders recovery before
any publisher that has not started its write; the repair re-reads and row-locks the candidate
before touching the store.

For an expired `pending` row:

- a missing object causes the row to be deleted;
- a present object whose HEAD size and SHA-256 equal the persisted `size_bytes` and `digest` is
  registered; a persisted config key is retained only when its object HEADs, and is cleared when
  absent;
- a present object with missing or mismatched integrity evidence is deleted, its absence is
  confirmed with another HEAD, and only then is the row deleted.

Every image qcow2 PUT supplies its base64 SHA-256 through `ChecksumSHA256`, so later HEAD requests
can make the integrity decision without downloading a potentially multi-gigabyte image. Objects
written before this decision with no stored checksum are unverifiable and follow the invalid-object
path. A config HEAD error preserves the pending row for retry. Registered rows retain the existing
behavior: a missing object is removed after the deadline, while a present object is not revalidated
by this repair.

For a valid abandoned private image, the repair reads the persisted initiating principal and emits
the existing `private-upload:registered` audit event in the same transaction as the registration
flip. A private pending row without a principal is unverifiable state and is reclaimed rather than
registered. Public image recovery needs no project audit, matching normal public publication.

Private-image expiry no longer selects `pending` rows. Publication recovery is their sole automatic
deletion owner; once it registers an already-expired private image, the next expiry pass can prune
the registered row normally. This narrowly supersedes ADR-0093's all-private-row expiry rule and
prevents the TTL path from bypassing the publication fence.

Config inventory also yields runtime ownership while a config-managed row is `pending`. Its update
may still change config-owned fields independently, but runtime realization fields use a two-sided
compare-and-swap. They update only when the loaded snapshot was not `pending` and the database row's
current `(state, publication_attempt_id)` still equals that snapshot. A miss preserves
`object_key`, `kernel_config_key`, `digest`, `size_bytes`, publication fields, and `state`; the next
inventory pass re-reads and recomputes. This protects both a reservation committed after inventory
loaded `defined` and a registration committed after inventory loaded `pending`. Prune re-reads under
row lock and skips `pending`. Publication recovery removes a failed attempt; after successful
registration, a later inventory pass may update or prune the row normally. This narrowly refines
ADR-0112's config ownership.

Store errors abort the candidate transaction and preserve the row for a later pass. If deletion
returns but the follow-up HEAD still sees the object, the row is also preserved. A crash after
object deletion but before row deletion leaves a pending row with a missing object, which the next
pass removes. A crash after the publisher PUT but before registration releases the advisory lock
and leaves a valid object that the next pass registers.

Database-session loss or async task cancellation while the blocking PUT thread remains alive is a
safe terminal publication failure, not an active-publisher guarantee: either condition may release
the fence while the thread continues, and recovery may remove the row, but the attempt-specific key
prevents the late PUT from recreating the reclaimed key. That late object is rowless and the
existing leaked-image sweep removes it after grace. The publisher cannot register without
revalidating the same persisted attempt.

## Consequences

- A publisher whose database session and async task remain live is protected for the duration of
  its write without sizing or renewing another deadline. Session loss or cancellation fails the
  publication safely and may leave a bounded rowless object for the existing leaked-image sweep.
- The fence holds one database connection and one session advisory lock across a potentially long
  PUT, but no database transaction and no project-wide lock.
- Recovery registers only byte-identical objects and releases abandoned private-image quota after
  invalid or absent objects are reclaimed.
- Recovered private registrations preserve normal audit attribution, and recovered rows never
  advertise a missing kernel-config sibling.
- Recovery still relies on the object store's existing HEAD-after-delete visibility contract. It
  does not claim deletion complete while HEAD still observes the key, and the rowless leaked-image
  sweep remains a backstop after a committed row removal.
- The existing same-identity writer ordering problem is not broadened here. This fence orders a
  publisher against the reconciler; it does not redefine the catalog uniqueness contract for two
  independent publications.
- Automatic TTL and config-inventory deletion paths yield to pending-publication ownership, so the
  dangling repair is the only automatic path that can remove an active reservation.

## Considered & rejected

**Persist a publisher heartbeat and lease expiry.** This creates a second deadline whose renewal
must remain live while a blocking SDK call runs. Attempt-specific keys would make expiry safe, but
the heartbeat state and renewal complexity buy no required behavior over the session fence: either
mechanism safely fails the attempt when its database liveness is lost.

**Hold a row lock and database transaction across the PUT.** This gives the necessary ordering but
keeps a long-lived transaction snapshot during a transfer and can delay vacuum cleanup globally.
A session advisory lock has the same backend-death release property without the long transaction.

**Trust object presence and register every expired pending row.** Presence proves neither identity
nor completeness. Registering a partial or overwritten object would make the catalog claim bytes
that later materialization rejects.

**Always delete expired pending rows and let the leaked-object sweep clean up.** This discards a
complete object after the only missing step was a catalog flip and adds a rowless-object window.

**Keep the current behavior.** This leaves valid pending objects and their private quota immortal,
and it retains the independent slow-publisher race. It does not meet either requirement.
