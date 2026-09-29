# 0620 Authority-owned System teardown

## Status

Accepted (2026-09-06)

## Context

External-boot host domains, overlays, and recovery data can be private to the
authority host. Ordinary worker provisioning resolves its own libvirt URI and
cannot safely tear down that state. A completed external release stops
restricting admission but does not transfer host ownership.

## Decision

Systems with durable external-boot history use an authority-marked TEARDOWN
job selected from the newest activation's durable provider route. The local
authority performs host destruction inside its authenticated commit lane. The
worker commits only authority journal facts; it never receives a provider
mutation capability.

`torn_down` is an external-boot activation state for physical System teardown.
It is not `abandoned`. Cleanup and capacity accounting remain separately proven:
ready reservations credit once after cleanup, pending reservations never credit,
and quarantine retains ownership until authority disposition completes. A clean
release keeps its original release and cleanup evidence.

### Amendment (2026-09-28): an unresolved teardown owns the System (#2884)

Only a teardown may allocate authority for a System while any of these holds: a teardown
authority is `allocating` or `current`; the journal head is a teardown operation at `admitted`,
`mutation-started`, `provider-returned` or `observed`; the head carries a suspended teardown.
Any other purpose gets `superseded` and writes no authority row (migration 0161). Before this,
only a `current` teardown authority fenced allocation. An activate could supersede a teardown
takeover in flight and recover the teardown under an activate authority, with no teardown
commit or credit.

A public `systems.teardown` re-runs a `failed` authority teardown job when the marker it would
enqueue is identical. It resets that job to a fresh queued attempt under the System lock. Live,
`succeeded` and `canceled` jobs, and a marker mismatch, still replay. Authority rows are looked
up by id, generation, and a live state, never by `(job_id, job_attempt)` alone. A reset attempt
counter therefore cannot revive a superseded row, and the reservation still credits once.

A teardown's operation identity is fixed per activation and its attempt id per identity, so a
reclaimed or new teardown takes over with the interrupted teardown's identity. ADR-0584's
same-operation integrity rule binds only the operation's own phase records (`admitted` through
`terminal`), not `watermark-installed`, `takeover-superseded` or `takeover-acknowledged`, which
keep their own fences; the takeover anchors and recovers the suspended teardown. Recovery
observation and the final proof select the `mutation-started` record of their own generation.

Rejected for this amendment:

- **Fence only a `current` teardown authority (no change).** verified: the #2880 live proof
  (PR #2883) left an activate authority allocating generation 5 over the generation-4 teardown
  takeover.
- **A new job per public teardown.** verified: `jobs.dedup_key` is unique (migration 0001), and
  the teardown dedup key is fixed per System.
- **Give the takeover record the suspended operation's attempt id.** verified:
  `AuthorityTakeoverRequestV1` has no attempt field (`protocol.py`, commit 727de823e), and the
  worker sends the takeover before it can read the head.
- **Recycle every terminal teardown job.** judgment: a `succeeded` teardown already credited the
  reservation, and re-running it only adds a replay path to the exactly-once rule.

## Consequences

The server fails closed when historical authority routing is unavailable.
Authority commit recovery, rather than read-only observation, must resume an
interrupted host teardown. Migration 0147 widens the activation state and
teardown completion constraints.

## Considered & rejected

- **Worker calls ordinary provisioning teardown.** **verified:**
  `providers/local_libvirt/lifecycle/provisioning.py` builds its connection from
  `KDIVE_LIBVIRT_URI`, while authority composition receives a fixed provider
  socket. That can address a different daemon and bypasses authority ownership.
- **Reuse `abandoned`.** **verified:** the activation model requires abandoned
  terminal evidence with outcome `abandoned`; physical teardown does not prove
  that outcome.
- **Make recovery observation resume destruction.** **verified:** observation
  is a read-only classification boundary. Resuming a mutation there would give
  a recovery read capability destructive semantics.
