# 0710 — A local external-boot session opens its artifact root on first use

## Status

Accepted (2026-09-29)

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

### Amendment (2026-09-30): cleanup quarantine and finalization run under the session (#3012)

This amendment replaces the last Consequences bullet's "They stay session-free here".
`LocalLibvirtExternalBoot.record_cleanup_quarantine` and `finalize_cleanup_tombstone` now check
the proof, then open `self._io.open(authority, _expected_binding(recovery.binding))` and make the
store write inside it, like the other activation ports. Because session open creates nothing, the
pinned shape leaves exact recovery absence provable after cleanup.

The added precondition is session open itself (lane pin, owned domain, overlay), not the lease:
the authority adapter issues one lease for each offloaded call. On an operation's first
execution, every production caller has just opened a session on the same binding while the
authority's System lane is active: `cleanup` before quarantine (and `recover` and `cleanup` before
System-teardown quarantine); `cleanup_is_accounted` before System-teardown finalization, which
precedes `teardown_system`; and `cleanup_receipt`, or the commit's `cleanup` and the `observe`
after it, before `finalize`. ADR-0586's post-delete replay of an absent tombstone still succeeds
while the System exists, because the store branch is unchanged and finalization does not touch
the domain. A missing owned domain or overlay, or a failed libvirt connection or definition read
at the open, now makes the write fail closed as
`provider_conflict` instead of succeeding without a pin. That includes a terminal replay holding
only `lane.lock` that pops a pending point which outlived its request, after a System teardown has
destroyed the domain. After any authority restart, the same replay already fails through
`cleanup_receipt`.

- **Pin only when the lease resolves, otherwise write without a session.** verified: the
  production lease cannot fail to resolve inside an offloaded call (`LocalOperationLeaseScope.issue`
  in `LocalExternalBootAuthorityAdapter._offload`, `external_boot_authority.py` at `b1a8cd8c6`), so the
  fallback guards nothing reachable and would only reintroduce the unpinned write.
- **Keep both writes session-free.** judgment: the #2898 reason no longer holds, and the unpinned
  write was a recorded residual, not a required property.

### Amendment (2026-10-01): recovery-object delete and adopt write under the session (#3045)

`LocalLibvirtExternalBoot.delete_recovery_object` and `adopt_object` keep `observe_object` and its
digest comparison first. They then re-read the quarantine receipt and make their store write
(`finalize_tombstone` or `adopt_cleanup_quarantine`) inside
`self._io.open(authority, _expected_binding(binding.binding))`, like the cleanup ports above. The
final `observe_object` still runs after that session closes.

The added open adds no new precondition. The orphan service observes the object before calling
either port, and inside the port `observe_object` opens a session on the same binding, under the
same lease and System lane, both before and after the write. The port succeeds only if the last
open does. A missing domain or overlay therefore already fails the call; a replay after a System
teardown fails at the service's first observation and never reaches the write. The one changed
outcome is an open failure confined to the write session: the port now fails without writing,
and the next disposition attempt replays against the unchanged receipt.

- **Pin only when the session opens, otherwise write without one.** verified: every successful
  call already opens sessions before and after the write (`orphan.py` `resolve_selected`,
  `external_boot.py` `delete_recovery_object` and `adopt_object` at `780a4bfd8`), so the fallback
  guards nothing reachable.
- **Move the observation into the write session.** judgment: one session instead of three, but it
  changes `observe_object`, which this change does not own.
