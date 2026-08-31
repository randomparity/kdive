# 0586 — Local external-boot recovery uses an owned host directory

## Status

Accepted (2026-08-30)

## Context

ADR-0583 requires local-libvirt external boot to preserve the exact inactive domain definition and
the prior release-qualified module tree before replacing either, then restore both after worker
loss. The existing installer can replace the module tree through libguestfs and redefine the
domain, but has no durable capture or recovery-object primitive. The provider needs a bounded
representation whose ownership survives process restart without exposing host paths through the
shared `ExternalBootPorts` contract.

The operator selected a deterministic provider-host recovery directory on 2026-08-30. After review
showed that the shared port exposed no activation identity, the operator selected a narrow
provider-neutral activation binding on the same date. After cleanup review exposed the need for
post-commit retry evidence, the operator selected reordered finalization and then U1a: absence is
completion only for the exact current-binding finalize operation durably journaled as
`mutation-started`. This record settles those storage and ownership choices;
authority-reference translation, authority-service composition, finalization invocation, and
capability advertisement remain #2140.

Implementation found that migration 0121's immutable activation-ledger CHECK addresses the removed
`RecoveryPoint.ownership` path. On 2026-08-30 the operator authorized the next available additive
migration, 0124, to replace that CHECK without rewriting stored data.

Task 2 exposed that a relative archive filename cannot by itself authorize restoration: the guest
writer neither owns the recovery root nor receives a path from the closed capture descriptor. On
2026-08-30 the operator selected a symmetric owner-bound archive source rather than giving the
guest writer path-resolution authority.

After Task 2 implementation review, the operator selected a second boundary on 2026-08-30: Task 2
is pure validated module-tree I/O over a caller-provided authenticated guest-tree capability. It
owns no persistent staging namespace or restart evidence. Task 3 owns guest staging names,
publication, durable phase evidence, and crash recovery.

## Decision

Local-libvirt stores each recovery point beneath its configured provider-owned recovery root in a
directory selected only from canonical System and activation UUIDs. The shared opaque reference is
a versioned ownership token containing those UUIDs, never a path. Resolution reconstructs the path
under the configured root and rejects a token whose embedded owners differ from the recovery point.

`ExternalBootPorts.prepare` replaces its current internal pre-release signature with
`prepare(materialization, binding, authority)`, where closed `ExternalBootActivationBinding`
contains exactly canonical `system_id`, `run_id`, and `activation_id`. System and Run must equal the
materialization ownership. All six operations retain the opaque authority argument; the binding is
ownership, never authority. There are no verified external callers and fault-inject is the only
current implementation, so replace-by-default applies: update the protocol, fault-inject provider,
contract tests, and direct internal consumers atomically with no compatibility overload. #2140 alone
translates an authenticated authority request into this binding for production composition.
`RecoveryPoint.binding` replaces `RecoveryPoint.ownership`; the binding is the point's sole System,
Run, and activation owner representation. Every later operation compares the complete closed point
— binding, recovery reference, plan identity, materialization identity, source state, and target
state — with reopened canonical metadata before observation or mutation. Reopened
metadata, not caller-presented fields, supplies mutation truth. Any field mismatch is conflict with
no provider write. This adjacent value change lets `activate`, `observe`, `recover`, and `cleanup`
authenticate the selected owner and immutable state without decoding authority data.

Migration 0124 drops and recreates only `external_boot_activation_evidence_ownership`. Its recovery
point arm requires `binding` to be a JSON object whose key set is exactly `system_id`, `run_id`, and
`activation_id`, then requires those three UUIDs to equal the ledger row; the materialization and
evidence arms remain unchanged. A preflight aborts when any stored recovery point carries legacy
`ownership` or a missing/malformed binding, because this migration performs no data rewrite. It adds
no table, role, or grant. This capability is pre-release and unadvertised. Application rollback with
forward migration history is safe only before any binding-shaped recovery point is written; after
the first such write, rollback is roll-forward because the old application cannot deserialize it and
its legacy writes fail the new CHECK. Legacy `ownership` payloads receive no compatibility path.

This record refines ADR-0583's deterministic-prepare statement: identical retries within one
activation return the same recovery point, while distinct activation UUIDs intentionally produce
distinct points even when System, Run, plan, and source state repeat. A completed older activation
therefore cannot alias a later activation's recovery object.

Preparation creates an owner-only staging directory beside the final directory. It captures the
exact inactive XML bytes, their ADR-0583 canonical preserved and boot-projection digests, prior
power state, release, plan and materialization identities, and either an explicit absent module
state or a bounded tar archive plus canonical manifest of `/lib/modules/<release>`. Metadata is
closed canonical JSON. Files are written with no-follow, exclusive-create operations; each file and
the staging directory are fsynced before one atomic rename publishes the final directory, followed
by a parent-directory fsync. An existing final directory is reusable only when every owner,
identity, digest, size, and manifest matches.

A narrow libguestfs seam captures, observes, installs, and restores exactly one validated module
tree through a caller-provided `AuthenticatedGuestTree` capability. Task 3 constructs the capability
only after authenticating the System, verifying the domain inactive, opening the owned overlay, and
selecting either the live release tree for read-only capture/observation or a private Task-3-owned
staging tree for install/restoration. Before handing out a mutable staging capability, Task 3
durably writes and fsyncs intent binding its deterministic staging name, System, activation,
release, operation, and expected identity. The capability exposes structured tree operations, never an
overlay path, guest path, staging name, rename, or phase-evidence operation. Task 2 creates no
persistent `.kdive-partial`, `.kdive-previous`, or equivalent namespace and classifies no restart
phase.

Capture receives an owner-bound `RecoveryArchiveSink`; restore has the exact signature
`restore(tree, release, capture, source)`, where `tree` is the authenticated private staging-tree
capability, `capture` is the closed descriptor for that release, and `source` is the matching
owner-bound `RecoveryArchiveSource`. Local external-boot code
constructs both only after authenticating the recovery token and resolving its directory beneath
the configured root. One source owns the already-open authenticated recovery-directory descriptor
for one restore operation. It opens only the capture descriptor's relative archive filename via a
descriptor-relative no-follow lookup, then retains the opened archive descriptor so a concurrent
rename or symlink substitution cannot redirect the read. It requires source owner, release, and
capture identity to agree before reading and applies regular-file, service-owner, private-mode,
reservation-size, archive-entry, and uncompressed-byte bounds before guest mutation. It accepts and
returns no host path, closes every archive and directory descriptor on success or error, and exposes
no path-selection operation. Lookup, identity, metadata, or bound rejection completed before restore
starts is a conflict with zero tree mutation. A read or close failure after writes begin stops
further Task 2 mutation and returns failure without publication authority. Task 3 retains ownership
of the staging capability and updates its already-durable intent with the observed partial phase.
If the process dies inside Task 2, the pre-call intent lets a fresh Task 3 instance authenticate and
classify the deterministic staging tree. Task 3 alone decides whether retry resumes or removes it.

Capture and restore use the ADR-0583 limits of 200,000 entries and 8 GiB uncompressed content.
Task 2 validates the manifest and writes only through the supplied staging-tree capability. It never
renames that tree into the live release or emits durable phase evidence. On 2026-08-30 the operator
selected existing libguestfs `mv` rather than a guestmount or appliance helper. The resulting bounded
live-name absence is allowed only while Task 3 has verified the domain inactive; boot, readiness,
and lifecycle advancement remain forbidden until a complete desired tree or exact desired absence
has been verified and durable publication evidence is complete.

For a present desired tree, Task 3 verifies live=prior, staging=desired, and old-aside absent, then
guest-syncs and fsyncs `move-ready` evidence. It moves live to the deterministic old-aside name with
libguestfs `mv`, guest-syncs, verifies live absent and old-aside=prior, and fsyncs `old-aside`
evidence. This is the only permitted live-name absence window. It then moves staging to live,
guest-syncs, verifies live=desired and old-aside=prior, and fsyncs `new-live` evidence. Only after a
complete desired live-tree verification does it remove old-aside, guest-sync, and fsync
`publication-complete` evidence. A present-empty desired tree follows the same path.

If the staging-to-live move fails while live is absent, Task 3 rolls back by moving authenticated
old-aside to live, guest-syncing, verifying live=prior and staging=desired, and fsyncing
`rollback-complete`; it never boots the rollback state as the requested result. Restart compares the
complete identities at all three deterministic names rather than trusting the last phase alone:
live=prior/staging=desired/old absent is pre-move or rolled back; live absent/staging=desired/old=prior
is old-aside; live=desired/staging absent/old=prior is new-live; every mixed, missing, duplicated,
unowned, unreadable, over-limit, or third layout is conflict with the domain kept inactive. The
single additional terminal layout live=desired/staging absent/old absent is post-removal with
completion evidence pending. After re-verifying the complete desired identity, restart may only
fsync `publication-complete`; it performs no further guest mutation. Boot and readiness remain
forbidden until that evidence is durable.

Restoring recorded absence uses the same first move without a staging-to-live move. After
`move-ready`, Task 3 moves live to old-aside, guest-syncs, verifies live absent and old-aside=prior,
and fsyncs `absence-live`; exact absence is then the verified desired state. It removes old-aside only
after fsyncing `absence-complete`. An already-absent live tree is verified and records
`absence-complete` without a move. A crash between a guest move and its evidence fsync is classified
from the three names and complete manifests. No guestmount, `renameat2`, appliance command helper, or
new provisioning dependency is introduced.

Cleanup removes payloads only from a directory whose canonical metadata proves the exact System and
activation owner. It then atomically replaces metadata with a canonical `cleaned` tombstone that
retains the complete point identity and explicit payload-absence facts, and fsyncs the directory and
parent. The tombstone remains recovery evidence and keeps its reservation charged. #2108 defines an
idempotent local `finalize_cleanup_tombstone(point, proof, authority)` primitive. Closed
`FinalizeCleanupProof` contains the complete point digest, current activation binding, exact
finalize operation and attempt identities, authority-journal sequence and digest, and literal
`phase="mutation-started"`. #2140 alone authenticates the current binding and journal head and
constructs this proof; local-libvirt compares its closed fields but never decodes authority.

With a present tombstone, finalization requires exact proof/point/tombstone equality, deletes only
that directory, verifies absence, and fsyncs the parent. With an absent tombstone, it succeeds only
when #2140 re-presents the same proof for the exact still-current finalize operation whose
`mutation-started` record is durable. Generic absence and a stale, superseded, cross-binding,
cross-operation, or unjournaled proof are conflict. This narrowly resolves a crash or lost response
between deletion and journal terminalization without treating absence generally as success. After
successful or confirmed finalization, #2118/core releases the reservation and commits
`cleanup_complete`; neither may occur earlier. Production advertisement is blocked until that
ordering is wired and tested. There is no post-cleanup receipt, generic sweeper, retention timeout,
or separate tombstone budget.

#2140 also owns terminal-result replay. If finalization has already reached an anchored terminal
journal record but its response was lost, an identical operation identity, attempt, request digest,
authority binding, and terminal chain returns the recorded observation without admitting a new
mutation or invoking local-libvirt. Only an unresolved exact `mutation-started` operation re-presents
the U1a deletion proof. A different retry is a new operation and cannot treat absence as its success.
Teardown likewise quarantines evidence it cannot authenticate.

## Consequences

- Local recovery remains host-local and needs no object-store credential or network availability.
- Recovery capacity is bounded per activation but can remain charged while conflict evidence is
  quarantined; the owning lifecycle remains responsible for admission and release accounting.
- One bounded cleanup tombstone remains reservation-owned after payload deletion so retries
  distinguish completed cleanup from unowned absence. Capacity release follows authenticated
  finalization, never tombstone creation; core cleanup completion follows capacity release.
- #2140 gains the integration obligation to construct the current journal proof and call
  finalization before #2118/core releases capacity and commits cleanup completion. Advertisement is
  withheld until crash-ordering, stale-proof, unresolved-operation recovery, and terminal-result
  replay tests pass.
- The configured recovery root becomes durable provider state and must share the worker's lifecycle,
  permissions, backup expectations, and provisioning parity on x86_64 and ppc64le hosts.
- The design adds a fixed libguestfs recovery seam but no generic guest filesystem editor.
- The internal pre-release shared port changes incompatibly. Current in-tree fault-inject and test
  consumers move in the same change; any newly discovered external consumer requires a scope
  checkpoint rather than a silent compatibility shim.
- Shared `RecoveryPoint` replaces its System/Run `ownership` field with the activation binding.
  Serialization and contract tests reject the removed field, missing bindings, and cross-activation
  token substitution; no legacy serialized point exists in production because the capability is not
  advertised.
- Migration 0124 is the additive database consequence of that replacement. Migration inventory,
  immutability, exact object/key-set CHECK shape, preflight abort, valid binding persistence, and
  legacy/malformed/cross-owner rejection are tested explicitly.
- Archive-source contract and adversarial tests freeze the exact restore signature and prove
  cross-owner/release/capture rejection, absolute or traversal-name rejection, symlink and rename
  substitution resistance, non-regular/foreign-owner/non-private/over-limit rejection, bounded
  short/error reads, host-path opacity, and close on success and error. Tests distinguish
  pre-write rejection with zero tree mutation from post-write read/close failure, which must stop
  Task 2 mutation. Task 3 tests prove those failures cannot publish the live release and that the
  caller-owned partial is durably retry-classifiable.
- Task 2 unit tests use an in-memory authenticated-tree fake and prove it requests no overlay path,
  guest path, staging name, rename, fsync-phase, or restart-classification operation. Task 3 owns
  integration and crash tests for the libguestfs moves, guest-sync/evidence-fsync ordering, rollback,
  and restart immediately before/after each move, sync, evidence fsync, and removal. Those tests
  cover present-empty and absent desired trees, bound the live-name absence to verified inactivity,
  distinguish the post-removal/evidence-pending layout, forbid boot/readiness until verified durable
  completion, and remove old-aside only after matching new-live evidence.

## Considered & rejected

- **Use a qcow2 external snapshot as the recovery point.** judgment: this couples one release-tree
  rollback to overlay-chain ownership, provisioning, reaping, and block-layer capacity when the
  narrower owned directory satisfies the accepted contract.
- **Store recovery bytes in the object store.** judgment: this adds credentials, network
  availability, and a second cleanup owner to a provider-host operation that can remain local.
- **Reuse only the current installer and reconstruct prior state later.** verified:
  `src/kdive/providers/local_libvirt/lifecycle/boot/guest_kernel_writer.py` removes the existing
  release directory before injection and exposes no capture operation, so the prior bytes cannot be
  reconstructed after replacement.
- **Put a host path in `OpaqueProviderRef`.** verified: ADR-0583 requires provider paths to remain
  behind the provider seam, and `OpaqueProviderRef` rejects absolute and traversal-bearing values in
  `src/kdive/providers/ports/external_boot.py`.
- **Let `GuestRecoveryWriter` resolve the capture's relative filename.** judgment: this gives the
  guest filesystem primitive recovery-root authority it does not own and leaves the restore
  signature without an authenticated base directory.
- **Store an absolute archive path in `ModuleArchiveCapture`.** judgment: this exposes a provider
  host path through the shared recovery value and makes caller-replayed path bytes authoritative.
- **Decode the authority reference inside local-libvirt to obtain the activation.** judgment: this
  would collapse #2140's authentication/translation boundary into a provider primitive and make an
  opaque capability double as ownership data.
- **Key recovery by System, Run, and plan instead of activation.** judgment: it contradicts the
  selected stable activation ownership and permits two activation records for the same immutable
  plan to address the same recovery object.
- **Do nothing and keep local-libvirt on its embedded installer path.** judgment: it cannot meet
  issue #2108's shared-port and crash-recovery acceptance criteria.
