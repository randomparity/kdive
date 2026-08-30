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

A narrow libguestfs seam captures, observes, installs, restores, and removes exactly the validated
release directory on the System overlay. It accepts structured values, not caller paths or
commands. Capture and restore use the ADR-0583 limits of 200,000 entries and 8 GiB uncompressed
content. Restoration stages beside the release directory, verifies its manifest, and uses
same-filesystem renames with durable phase metadata so retry can classify source, target, and the
provider-owned partial state. Any unowned, unreadable, over-limit, or third state is a conflict and
causes no further mutation.

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
- **Decode the authority reference inside local-libvirt to obtain the activation.** judgment: this
  would collapse #2140's authentication/translation boundary into a provider primitive and make an
  opaque capability double as ownership data.
- **Key recovery by System, Run, and plan instead of activation.** judgment: it contradicts the
  selected stable activation ownership and permits two activation records for the same immutable
  plan to address the same recovery object.
- **Do nothing and keep local-libvirt on its embedded installer path.** judgment: it cannot meet
  issue #2108's shared-port and crash-recovery acceptance criteria.
