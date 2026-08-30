# Local-libvirt external-boot design

## Scope and governing decisions

Issue #2108 adapts local-libvirt to ADR-0583's `ExternalBootPorts`. ADR-0586 records the
user-selected provider-host recovery directory and the later A1 decision to carry a closed
provider-neutral activation binding into preparation. The implementation covers the shared value
and signature adjustment, local provider primitives, fault-inject consistency, and tests only.
`AuthorityMutationAdapter`, authority-reference translation, provider-host service composition,
post-core tombstone-finalization invocation, configured authority coordinates, and capability
advertisement remain #2140; remote primitives, jobs, reconciliation, and hosting remain excluded.
The sole schema change is additive migration 0124, authorized by the operator on 2026-08-30 after
implementation exposed migration 0121's immutable CHECK dependency.

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

Migration 0124 replaces only the existing `external_boot_activation_evidence_ownership` CHECK. The
new recovery-point arm requires `jsonb_typeof(binding) = 'object'`, requires its key set to equal
exactly `system_id`, `run_id`, and `activation_id`, and requires those three UUIDs to equal the
activation row; all other arms are preserved. Before dropping the old CHECK, the same migration
requires the exact normalized 0121 definition with `convalidated=true`, then scans stored recovery
points. Under that provenance, legacy `ownership` is the only incompatible non-null recovery-point
shape 0121 admits; its presence aborts. Missing, scalar/array, extra/missing-key, malformed-UUID, and
cross-owner binding shapes are defensively rejected by the replacement CHECK after migration. An
abort preserves the old constraint because the migration runner is transactional. Migration 0124 rewrites no data and
creates no role or grant. Application rollback with immutable forward migration history is safe only
before the first binding-shaped recovery point is written. After that write, recovery is roll-forward:
the old application cannot deserialize the new point and its legacy write fails the replacement
CHECK. The removed payload contract is never restored.

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
Its `recovery-module-tree-v1` manifest uses ADR-0583's exact canonical JSON bytes and
`kdive-recovery-module-tree-v1` NUL-prefixed hash domain: entries sort by UTF-8 path bytes; object
keys, UTF-8/control escaping, four-character lowercase octal modes, integer fields, lowercase
content digests, and no-trailing-newline encoding are identical to `module-installed-tree-v1`.
Xattr names sort bytewise and map to unpadded standard base64 values. Entries carry relative NFC
path, kind, mode, uid, gid, size and content SHA-256 for regular files, every xattr including POSIX
ACL and `security.*` values, and `xattrs_supported`. Symlink lstat
targets are recorded verbatim as UTF-8 NFC and may be absolute; capture, hashing, and restore never
follow them. Hard links, special files, undecodable names/targets, noncanonical entry paths,
duplicates, more than 200,000 entries, or more than 8 GiB of regular content reject before
publication. Observation uses the same walk and manifest algorithm.

The envelope is exactly `{"entries":[...],"schema":"recovery-module-tree-v1"}`. An absent release
uses `AbsentComponentState` and has no manifest; an existing empty directory uses an empty entries
array whose domain-separated digest is
`sha256:7048c9e065ecf77a964188f42aaebb79a3e8238ecc47736ae47239b8ceec30a5`.
Golden fixtures freeze complete bytes and digests for a regular file, `xattrs_supported=false`, an
unpadded-base64 ACL/security xattr set, and an absolute `build` symlink. Unsupported xattrs record
`xattrs_supported=false` and `{}`; an error after support was established is unreadable conflict,
not unsupported. Timestamps are excluded; uid, gid, and lstat permission bits are preserved exactly.

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

`prepare` reads the domain's initial active state and inactive XML, safe-parses and verifies KDIVE
System ownership, computes ADR-0583 definition identities, and durably publishes a `pre-stop-intent`
inside the owned partial directory before any power mutation. The intent binds the complete point
inputs, prior power state, exact XML bytes/digests, and source boot projection; its file, directory,
and parent are fsynced. Only then does prepare use the existing bounded force-off operation and
verify inactivity before opening the overlay read-write. It renders the target by changing only
`/domain/os/kernel`, optional `initrd`, and `cmdline`. It then captures the exact source release tree through
`GuestRecoveryWriter.capture`, verifies the source state by a fresh observation, and builds the
target module tree without publishing it. If preparation fails after stopping but before publishing
the recovery point, it restores the captured definition/module state when available and restores
the recorded power state; otherwise it retains the owned partial for retry and reports failure.

The recovery directory is staged as `<root>/.<system>.<activation>.partial` with mode 0700 beneath a
pre-existing owner-only recovery root. Regular files are 0600 and opened no-follow/exclusive. After
mkdir and parent fsync, `pre-stop-intent` is the first entry and is durably written before stopping.
A crash between mkdir and intent may leave only an empty directory; retry removes it only when lstat
proves the exact deterministic name, service uid, mode 0700, directory type, and zero entries.
Anything else is quarantined. Once intent exists, retry authenticates it and resumes without
recapturing prior power or XML. File, directory, rename, and parent fsync order follows ADR-0586. It
returns `RecoveryPoint` only after reopening final metadata and verifying every digest and owner.

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
tombstone remains reservation-owned recovery evidence. Add closed
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
crash-after-delete-before-terminal-journal, stale-proof, and lost-response tests pass. If the
authority already anchored a terminal record before losing its response, #2140 replays the recorded
terminal observation only when operation identity, attempt, request digest, binding, and terminal
chain are identical; it admits no new mutation and does not invoke local-libvirt. Only an unresolved
exact `mutation-started` operation may re-present the U1a proof. Tests cover both response-loss
windows independently. No receipt, generic reconciliation sweep, separate budget, or retention
timer exists. Cleanup never
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
Migration tests additionally freeze inventory order and migration immutability, inspect the exact
replacement CHECK, accept canonical binding rows, and reject missing, legacy, scalar/array,
malformed-UUID, extra-key, cross-System, cross-Run, and cross-activation recovery points without
changing role grants. Upgrade fixtures prove a reachable legacy row aborts migration and that an
exact-definition but `NOT VALID` 0121 constraint is rejected with no partial DDL. Compatibility assertions cover both
sides of the first-binding-write boundary: pre-write application rollback and post-write
roll-forward-only recovery.
