# 0710 — A local external-boot session opens its artifact root on first use

## Status

Proposed

## Context

[ADR-0587](0587-local-external-boot-uses-an-operation-session.md) has session construction open the
owner-bound artifact-root descriptor, and the production binding (`LocalArtifactRoot.open`, per
[ADR-0591](0591-local-external-boot-session-mechanisms-bind-to-the-recovery-root.md) and
[ADR-0602](0602-local-external-boot-storage-is-reclaimed-by-owned-identities.md)) creates
`<system>/<run>/<activation>` under the recovery root when absent. Every activation-scoped provider
port opens a session, including the read-only ones the external-boot authority uses to prove
recovery absence: `recovery_point`, `cleanup_receipt`, `observe_state`, `recovery_is_absent`, and
`abort_preparation` when it finds nothing to abort. `exact_recovery_absence` returns false while
the activation directory exists, so each read re-creates the state that makes absence unprovable.
A non-System TEARDOWN partial abort therefore ends in `provider_conflict` (#2926). PR #2902 fixed
the System-teardown path by keeping its absence and point checks session-free and pruning empty
parents after its abort, which still opens an activation session. The non-System path and
`observe_state` need the libvirt domain, so they cannot drop the session.

## Decision

We will open the artifact root lazily. Session construction retains the pinned ownership snapshot
and a bound opener, and makes no artifact-root call. The first session method that needs the
activation directory (projection directory, projection reopen, payload open/unlink, payload path,
transfer, or payload cleanup) opens it once under the session lifecycle lock; later calls reuse the
descriptor; close releases it only if it was opened. Session operations that never use it leave no
activation storage behind.

`abort_preparation` on the non-System path also prunes still-empty activation parents after it
returns `removed` or `absent`, as the System path already does. That converges empty parents left
by a session opened before this change, or by a materialization interrupted between the first
artifact open and its digest-directory `mkdir`. A materialization interrupted after that `mkdir`
leaves unauthenticated residue, which still quarantines as `provider_conflict`.

## Consequences

- Absence proofs and the authority's reads around them no longer write under the recovery root.
- A malformed activation directory (wrong mode, owner, symlinked component) is refused at the
  first artifact use instead of at session open. The recovery root itself is still checked first
  by the recovery store. Materialization is the first use of an activation and precedes every
  provider mutation. In a later `prepare` session the refusal can now follow the clean stop; the
  pre-stop intent is durable before that stop, so the existing partial-abort path restores the
  recorded power, as it does for any other `prepare` failure after the stop.
- The ownership snapshot passed to the opener is still the one the pin produced at open; a lazily
  opened root cannot be redirected by later changes to the caller's lease.
- A directory that holds anything is still left for quarantine and still keeps absence false.
- The #2898 reason for keeping `record_cleanup_quarantine` and finalization session-free no longer
  holds. They stay session-free here; moving them under the pinned session is a separate change.

## Considered & rejected

- **Make every read-only port session-free, as #2902 did.** verified:
  `_RealLocalExternalBootOperation.observe_state` calls `session.inspect_closed()` and
  `session.guest()`, and the pre-stop branch of `_abort_preparation` calls `inspect_closed()` and
  `restore_power()` (`external_boot.py` at `65a31d5aa`), so they need the domain session and would
  still re-create the directories.
- **Prune after every read-only port.** judgment: five call sites repeating a write whose only
  purpose is undoing another write, and any new read port reintroduces the defect.
- **Make `LocalArtifactRoot.open` open-only and create elsewhere.** verified: #2210 provisions only
  the recovery root, so an open-only walk refuses the first activation (ADR-0591 Decision).
- **Open an existing activation directory at session open and create it only on first use.**
  judgment: a second open mode on the `OpenArtifactRoot` seam to keep an early refusal that only
  a directory tampered with between sessions can trigger, which recovery already handles.
- **Treat an empty activation chain as absent in `exact_recovery_absence`.** judgment: weakens
  exact absence to "no owned content" and leaves empty directories under the recovery root for
  every read.
- **Do nothing.** judgment: a non-System TEARDOWN of a partial preparation can never complete.
