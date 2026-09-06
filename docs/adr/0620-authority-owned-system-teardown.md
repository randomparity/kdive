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
