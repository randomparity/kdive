# Local-libvirt external-boot design

## Scope and governing decisions

Issue #2108 adapts local-libvirt to ADR-0583's `ExternalBootPorts`. ADR-0586 records the
user-selected provider-host recovery directory and the later A1 decision to carry a closed
provider-neutral activation binding into preparation. The implementation covers the shared value
and signature adjustment, local provider primitives, fault-inject consistency, and tests only.
`AuthorityMutationAdapter`, authority-reference translation, provider-host service composition,
post-core tombstone-finalization invocation, configured authority coordinates, and capability
advertisement remain #2140; remote primitives,
jobs, reconciliation, hosting, schemas, and migrations remain excluded.

The implementation supports Python 3.14 on the declared x86_64 and ppc64le targets and adds no
dependency. It reuses libvirt, defused XML parsing, `xml.etree.ElementTree.canonicalize`, libguestfs,
the bounded kernel-bundle reader, staged writes, and existing readiness seam.

## Components and values

Create `local_libvirt/lifecycle/external_boot.py` containing `LocalLibvirtExternalBoot`, the six
synchronous `ExternalBootPorts` methods, ADR-0583 libvirt-definition identity helpers, recovery
metadata models, and filesystem publication. Keep legacy `LocalLibvirtInstall` unchanged; shared
helpers may move only when both callers need the identical behavior.

Add closed `ExternalBootActivationBinding` beside the shared values with exactly canonical
`system_id`, `run_id`, and `activation_id`. Replace the pre-release protocol signature with:

```python
def prepare(
    self,
    materialization: ExternalBootMaterialization,
    binding: ExternalBootActivationBinding,
    authority: OpaqueProviderRef,
) -> RecoveryPoint: ...
```

Preparation rejects unless binding System and Run equal materialization ownership. Binding is an
immutable owner coordinate, not proof of authority. No compatibility overload remains: repository
search identifies only fault-inject and tests as current consumers, and they update atomically. If
implementation discovers an external or separately versioned caller, it stops at scope checkpoint.
#2140 alone validates the authority protocol and constructs the binding in production.
`RecoveryPoint` replaces `ownership: ActivationOwnership` with
`binding: ExternalBootActivationBinding`; it never retains two owner representations. `prepare`
copies the exact input binding into the point. Every
recovery-consuming operation requires the complete closed point — binding, recovery reference, plan
identity, materialization identity, source state, and target state — to equal reopened canonical
recovery metadata before reading provider state. Reopened metadata supplies every expected identity
used for comparison and mutation; caller-presented point fields never override it. Contract tests
substitute each field independently, including a point and token across two activation IDs with the
same System, Run, plan, and source, and prove zero observation, mutation, or deletion.

Recovery identity is deterministic within one activation. A retry with the same binding, plan, and
source returns byte-identical point metadata and reference. A distinct activation ID always selects
a distinct point, deliberately refining ADR-0583's older System/Run/plan/source wording so a later
activation cannot alias completed evidence.

Create `local_libvirt/lifecycle/boot/recovery.py` with a narrow `GuestRecoveryWriter` protocol and
real libguestfs implementation. It owns only `/lib/modules/<validated-release>` and uses no shell or
guest executable. Its operations are:

```python
class GuestRecoveryWriter(Protocol):
    def capture(self, overlay: str, release: str, destination: Path) -> ModuleCapture: ...
    def observe(self, overlay: str, release: str) -> ComponentState: ...
    def install(self, overlay: str, release: str, source: Path) -> str: ...
    def restore(self, overlay: str, release: str, capture: ModuleCapture) -> str: ...
```

`ModuleCapture` is either explicit absence or an archive descriptor containing the canonical
manifest digest, entry count, uncompressed bytes, archive SHA-256, and relative archive filename.
Its `recovery-module-tree-v1` manifest is exactly ADR-0583's installed entry metadata and path
grammar: sorted relative NFC entry path, kind, mode, uid, gid, size and content SHA-256 for regular
files, every xattr including POSIX ACL and `security.*` values, and `xattrs_supported`. Symlink lstat
targets are recorded verbatim as UTF-8 NFC and may be absolute; capture, hashing, and restore never
follow them. Hard links, special files, undecodable names/targets, noncanonical entry paths,
duplicates, more than 200,000 entries, or more than 8 GiB of regular content reject before
publication. Observation uses the same walk and manifest algorithm.

`LocalRecoveryMetadataV1` is closed canonical JSON with schema, System and activation UUIDs, Run,
plan and materialization identities, release, exact source inactive XML SHA-256, canonical preserved
definition digest, source and target boot-projection digests, source and target module states, prior
power state, capture descriptor, and durable recovery phase. It contains no configured root or
absolute path. The opaque reference has `local-recovery-v1/<system UUID>/<activation UUID>` and is
accepted only when both UUIDs equal the supplied `RecoveryPoint.binding`.

## Materialize and prepare

`materialize` validates provider kind `local-libvirt`, ownership, architecture, bundle and initrd
digests already carried by `ExternalBootPlan`. It stages artifacts under the existing
System/Run directory using streamed kernel extraction and temp-then-rename initrd fetch, verifies
the extracted vmlinuz digest and module obligation, and returns opaque relative artifact refs plus
the exact running-kernel observation facts derived from the validated bundle. Retry accepts only
matching complete files; a partial or mismatched final artifact is removed only when its deterministic
System/Run ownership is provable, otherwise it is conflict.

`prepare` reads and records the domain's initial active state, then uses the existing bounded
force-off operation and verifies inactivity before opening its overlay read-write. It reads inactive
XML, safe-parses it, verifies KDIVE System ownership, and computes ADR-0583 preserved-definition and
boot-projection identities. It renders the target by changing only `/domain/os/kernel`, optional
`initrd`, and `cmdline`. It then captures the exact source release tree through
`GuestRecoveryWriter.capture`, verifies the source state by a fresh observation, and builds the
target module tree without publishing it. If preparation fails after stopping but before publishing
the recovery point, it restores the captured definition/module state when available and restores
the recorded power state; otherwise it retains the owned partial for retry and reports failure.

The recovery directory is staged as `<root>/.<system>.<activation>.partial` with mode 0700 beneath a
pre-existing owner-only recovery root. Regular files are 0600 and opened no-follow/exclusive. File,
directory, rename, and parent fsync order follows ADR-0586. A retry removes only its authenticated
partial directory. It returns `RecoveryPoint` only after reopening the final metadata and verifying
every digest and owner.

## Activate, observe, recover, and cleanup

`activate` reopens and authenticates the recovery metadata, requires a fresh complete source-state
observation, installs the target module tree with same-filesystem staged rename, records and fsyncs
the module-installed phase, rechecks the complete composite state, defines the target inactive XML,
and fsyncs target-defined evidence. It does not start the domain; the existing lifecycle owner keeps
boot and readiness responsibility. Retry classifies source, target, or its own durable partial
phase. A source/target mixture is resumable only when metadata proves the completed component write;
every other mixture is conflict.

The internal `_observe_composite(recovery)` helper reads inactive XML and the release tree
independently. It classifies source or target only when both component identities match the same
recorded composite state; it may classify a provider-owned partial phase for same-operation
resumption. Missing domain, malformed/forbidden XML, unreadable overlay, unowned metadata, or an
unclassified mixture is conflict, never absence. Public `ExternalBootPorts.observe` retains its
existing `RunningKernelObservation` return contract. It authenticates the complete point, requires
durable `target-defined` evidence produced by inactive activation, and then uses only the existing
bounded readiness/running-kernel seam to return architecture, release, and GNU build ID. It never
opens the live overlay or exposes internal composite classification. Fresh composite observation
occurs only while the domain is verified inactive in prepare, activate, and recover.

`recover` requires the authenticated recovery point and a source, target, or owned-partial state.
It restores the captured module tree (or verifies/removes it for recorded absence), fsyncs durable
module-restored evidence, defines the exact captured inactive XML bytes, and verifies the complete
source identity. It restores recorded power through existing control/readiness seams only after
both persistent components match source. A running prior state requires a fresh readiness success;
an inactive prior state remains inactive. Retry from complete source performs only the conditional
power restoration.

`cleanup` requires complete source state. It deletes materialized kernel/initrd/archive/XML payloads,
verifies absence, and atomically replaces metadata with a 0600 canonical `cleaned` tombstone that
retains the complete point identity plus explicit payload-absence facts. It fsyncs the tombstone,
directory, and parent before success. A lost-response retry authenticates the tombstone and returns
success without provider mutation; missing metadata without the exact tombstone is quarantined. The
The tombstone remains reservation-owned recovery evidence. Add closed
`FinalizeCleanupProof(point_digest, binding, operation_id, attempt_id, journal_sequence,
journal_digest, phase="mutation-started")` and the narrow local primitive
`finalize_cleanup_tombstone(point: RecoveryPoint, proof: FinalizeCleanupProof,
authority: OpaqueProviderRef) -> None`. #2140 authenticates the exact current binding and journal
head before constructing the proof; local-libvirt validates only closed equality and never parses
authority. A present tombstone is deleted only after complete point/proof/tombstone equality, then
absence and parent fsync are verified. An absent tombstone succeeds only when #2140 re-presents the
same exact still-current proof whose `mutation-started` record predates the attempted delete.
Generic, stale, superseded, cross-binding, cross-operation, or unjournaled absence is conflict.
#2140 calls finalization while cleanup remains incomplete and the reservation charged. Only after
success or exact U1a confirmation does #2118/core release the reservation and durably commit
`cleanup_complete`. Advertisement remains blocked until crash-before-delete,
crash-after-delete-before-terminal-journal, stale-proof, and lost-response tests pass. No receipt,
generic reconciliation sweep, separate budget, or retention timer exists.
Cleanup never
follows symlinks or deletes an object whose metadata owner does not exactly match. Destroyed-System
cleanup is not a second six-port entry point; teardown-owned invocation and authentication remain
outside #2108.

## Error and observability contract

Malformed plans, refs, XML, archives, or ownership mismatches fail before mutation. Libvirt,
libguestfs, filesystem, and readiness unavailability use existing bounded `CategorizedError`
categories; stable third-state or owner mismatches are conflicts. Diagnostics contain operation,
bounded category, System/activation identifiers, and digests only. They exclude XML, cmdline,
archive names, host paths, guest content, credentials, and raw tool output.

## Threat model

Added boundaries are validated plan/recovery values into local filesystem resolution; libvirt
inactive XML into canonical identity parsing; a stopped System overlay into libguestfs; and recovery
bytes read after process restart. Authenticated but stale workers may replay valid-looking refs;
tenants influence build artifacts and command-line values; a local operator controls configuration;
libvirtd and the provider host are trusted. Privileged host interference is outside the fence.

Opaque owner tokens are parsed as closed canonical components and resolved beneath a configured
root without accepting caller path bytes. Owner, plan, materialization, release, and digest checks
precede every write. Defused parsing rejects DTD/entity input; XML construction changes only three
owned fields. Archive capture and extraction enforce no-follow topology, NFC, entry and byte bounds,
and content manifests. Recovery files use owner-only modes, exclusive/no-follow creation, atomic
publication, and fsync. Failures reveal bounded identifiers and categories only. Authority freshness
is deliberately not reimplemented here: #2140 wraps these primitives in the ADR-0584 service before
advertisement. Until then no production composition exposes this port.

Out of scope are compromise of the trusted host/libvirtd/libguestfs appliance, privileged manual
disk edits, authority transport/authentication, remote providers, lifecycle database truth, and
capacity admission. Those are existing operator trust or owned issues, not claims of this design.

The #2140 integration acceptance must include exact current-binding proof construction and
finalization before #2118/core capacity release and `cleanup_complete`. Spellcraft records that
dependency here; changing #2140's public
issue body is not necessary to define or implement #2108 and is left to campaign tracking.

## Verification

Unit tests cover canonical XML vectors, definition preservation, opaque-ref ownership, archive
bounds/topology, absent/present manifests, optional initrd, atomic publication faults, every
source/target/partial/mixed observation, retries at each fsync/rename/define boundary, exact XML and
module restoration, prior-power readiness, cleanup/quarantine, and cross-System/Run/activation
denial with before/after snapshots. A controlled fault in owner comparison and manifest comparison
must make the new tests fail. Composition tests prove the capability is not advertised. Adversarial
tests interleave lost responses and restarts across component writes. Focused tests, `just lint`,
`just type`, `prek run`, and pre-push `just ci` remain required; live VM proof is deferred to the
existing manually dispatched tier and must not be claimed locally.
