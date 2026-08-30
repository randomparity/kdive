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
provider-neutral activation binding on the same date. This record settles those storage and
ownership choices; authority-reference translation, authority-service composition, and capability
advertisement remain #2140.

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

Cleanup removes only a directory whose canonical metadata proves the exact System and activation
owner, verifies absence, and fsyncs the parent. Teardown quarantines evidence it cannot authenticate.

## Consequences

- Local recovery remains host-local and needs no object-store credential or network availability.
- Recovery capacity is bounded per activation but can remain charged while conflict evidence is
  quarantined; the owning lifecycle remains responsible for admission and release accounting.
- The configured recovery root becomes durable provider state and must share the worker's lifecycle,
  permissions, backup expectations, and provisioning parity on x86_64 and ppc64le hosts.
- The design adds a fixed libguestfs recovery seam but no generic guest filesystem editor.
- The internal pre-release shared port changes incompatibly. Current in-tree fault-inject and test
  consumers move in the same change; any newly discovered external consumer requires a scope
  checkpoint rather than a silent compatibility shim.

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
