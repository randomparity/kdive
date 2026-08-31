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
For Task 2, the operator selected an owner-bound `RecoveryArchiveSource` on 2026-08-30 so restart
restoration never accepts or derives a provider-host path inside the guest writer.
The operator then selected a pure-I/O Task 2 boundary on the same date: Task 2 receives an
authenticated guest-tree capability and owns no persistent staging namespace, phase evidence,
publication, or restart classification. Task 3 owns those lifecycle operations.
At the publication-adapter checkpoint, the operator selected existing libguestfs `mv` with a
bounded live-name absence while the overlay is verified inactive; guestmount, `renameat2`, and an
appliance helper remain excluded.

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
    def capture(
        self, tree: AuthenticatedGuestTree, release: str, sink: RecoveryArchiveSink
    ) -> ModuleCapture: ...
    def observe(self, tree: AuthenticatedGuestTree, release: str) -> ComponentState: ...
    def install(self, tree: AuthenticatedGuestTree, release: str, source: Path) -> str: ...
    def restore(
        self,
        tree: AuthenticatedGuestTree,
        release: str,
        capture: ModuleCapture,
        source: RecoveryArchiveSource,
    ) -> str: ...
```

`AuthenticatedGuestTree` is a single-operation capability constructed and owned by Task 3 after
System ownership authentication, inactive-domain verification, and owned-overlay opening. It is
bound either to the live release tree for read-only capture/observation or to a private staging tree
for install/restore. Before passing a mutable staging capability to Task 2, Task 3 durably writes and
fsyncs intent binding its deterministic staging name, System, activation, release, operation, and
expected identity. It exposes only relative tree inspection and mutation needed by the canonical
walker; it exposes no overlay or guest path, staging name, live-tree rename/publication, durable
phase write, fsync-ordering decision, or restart-classification operation. Task 2 neither creates nor
recognizes persistent `.kdive-partial`, `.kdive-previous`, or equivalent names. Closing the tree and
deciding staging cleanup remain the Task 3 caller's responsibility after Task 2 returns.

`RecoveryArchiveSink` and `RecoveryArchiveSource` are symmetric owner-bound capabilities constructed
by `LocalLibvirtExternalBoot` only after it authenticates the recovery token and resolves the exact
System/activation directory beneath the configured recovery root. The sink exclusively stages and
fsyncs the fixed archive filename. Each source owns an already-open authenticated recovery-directory
descriptor for exactly one restore call and rejects reuse. Before its first read it requires the
source owner, requested release, and validated `ModuleArchiveCapture` identity to agree. It accepts
the relative filename only from that capture, opens it relative to the owned directory with
no-follow semantics, and retains the opened archive descriptor through the operation so a concurrent
rename or symlink substitution cannot redirect later reads. It verifies a regular owner-only file
owned by the service, rejects an archive larger than the configured recovery reservation before
parsing, returns a bounded read-only stream, and closes its file and directory descriptors on every
success and failure path. Neither capability accepts a caller path, returns a host path, or lets
`GuestRecoveryWriter` resolve one. An absent capture does not open the source.

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

Module restoration reads the owner-bound source into the caller-provided private staging-tree
capability. Lookup, identity, metadata, or bound failure before writes start causes zero tree
mutation. A source read or close failure after writes start stops further Task 2 mutation and
returns failure; Task 2 has no live-tree publication or persistent-phase operation, so it cannot
replace the live release. Task 3 then updates the pre-call durable intent with the observed partial
phase before returning the provider result. A crash inside Task 2 remains recoverable because a fresh
Task 3 instance authenticates the deterministic staging tree against that fsynced intent. Retry is a
Task 3 operation: it reopens and authenticates the
recovery point and source, then resumes that owned partial or removes it before a fresh attempt; it
never treats an unclassified or third partial as owned. On success, Task 3 independently verifies
the complete staged manifest. For a present desired tree it verifies live=prior, staging=desired,
and old-aside absent, guest-syncs, and fsyncs `move-ready`. It moves live to deterministic old-aside
with libguestfs `mv`, guest-syncs, verifies live absent and old-aside=prior, and fsyncs `old-aside`.
Only while the domain remains verified inactive may the live name be absent. Task 3 moves staging to
live, guest-syncs, verifies live=desired and old-aside=prior, and fsyncs `new-live`. It removes
old-aside only after complete desired-tree verification, then guest-syncs and fsyncs
`publication-complete`. A present-empty desired tree follows this path.

If staging-to-live fails while live is absent, Task 3 moves authenticated old-aside back to live,
guest-syncs, verifies live=prior and staging=desired, and fsyncs `rollback-complete`. Restart compares
complete identities at live, staging, and old-aside: prior/desired/absent is pre-move or rolled back;
absent/desired/prior is old-aside; desired/absent/prior is new-live. Any mixed, missing, duplicated,
unowned, unreadable, over-limit, or third layout conflicts with the domain kept inactive. A crash
between a move and evidence fsync is classified from names and identities, not phase alone.

Recorded desired absence uses only the first move. After `move-ready`, Task 3 moves live to
old-aside, guest-syncs, verifies live absent and old-aside=prior, and fsyncs `absence-live`. It fsyncs
`absence-complete` before removing old-aside. An already-absent live tree is verified and completes
without a move. Boot, readiness, and lifecycle advancement are forbidden until a complete desired
tree or exact desired absence is freshly verified and its completion evidence is durable. No
guestmount, `renameat2`, appliance helper, or new provisioning dependency is added.

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

Added boundaries are validated plan/recovery values into local filesystem resolution; an
authenticated owner-bound archive source into restart restoration; libvirt
inactive XML into canonical identity parsing; a stopped System overlay into libguestfs; and recovery
bytes read after process restart. Authenticated but stale workers may replay valid-looking refs;
tenants influence build artifacts and command-line values; a local operator controls configuration;
libvirtd and the provider host are trusted. Privileged host interference is outside the fence.

Opaque owner tokens are parsed as closed canonical components and resolved beneath a configured
root without accepting caller path bytes. The archive source is constructed only after that check,
opens the capture's canonical relative filename beneath the retained owner directory with no-follow
and bounded regular-file checks, and closes all descriptors deterministically. Owner, plan,
materialization, release, and digest checks
precede every write. Defused parsing rejects DTD/entity input; XML construction changes only three
owned fields. Archive capture and extraction enforce no-follow topology, NFC, entry and byte bounds,
and content manifests. Recovery files use owner-only modes, exclusive/no-follow creation, atomic
publication, and fsync. Failures reveal bounded identifiers and categories only. Authority freshness
is deliberately not reimplemented here: #2140 wraps these primitives in the ADR-0584 service before
advertisement. Until then no production composition exposes this port.

The selected publication sequence temporarily removes the live release name. Task 3 holds the
existing per-System serialized operation lane, verifies inactive immediately before each libguestfs
move, and keeps start/readiness unavailable until completion evidence and desired identity are
durable. A running or indeterminate domain aborts before opening the overlay or performing the next
move. Crash recovery repeats these checks before classifying or mutating live, staging, and
old-aside. The bounded absence is therefore observable only on an inactive overlay and is never a
bootable or completed state.

Out of scope are compromise of the trusted host/libvirtd/libguestfs appliance, privileged manual
disk edits, authority transport/authentication, remote providers, lifecycle database truth, and
capacity admission. Those are existing operator trust or owned issues, not claims of this design.

The #2140 integration acceptance must include exact current-binding proof construction and
finalization before #2118/core capacity release and `cleanup_complete`. Spellcraft records that
dependency here; changing #2140's public
issue body is not necessary to define or implement #2108 and is left to campaign tracking.

## Verification

Unit tests cover canonical XML vectors, definition preservation, opaque-ref ownership, archive
bounds/topology, absent/present manifests, optional initrd, move/publication faults, every
source/target/partial/mixed observation, retries at each fsync/rename/define boundary, exact XML and
module restoration, prior-power readiness, cleanup/quarantine, and cross-System/Run/activation
denial with before/after snapshots. A controlled fault in owner comparison and manifest comparison
must make the new tests fail. Composition tests prove the capability is not advertised. Adversarial
tests interleave lost responses and restarts across component writes. Focused tests, `just lint`,
`just type`, `prek run`, and pre-push `just ci` remain required; live VM proof is deferred to the
existing manually dispatched tier and must not be claimed locally.
The live proof matrix runs on operator-provided x86_64 and ppc64le local-libvirt hosts: present,
present-empty, and desired-absence publication; failure after live-to-old and before staging-to-live;
rollback; fresh-process recovery from `old-aside` and `new-live`; and a concurrent start attempt that
must remain refused until durable completion. Each arm proves the domain stays inactive throughout
the live-name absence window and boots only the freshly verified desired identity afterward. Report
each architecture arm as run, failed, or not run; emulation is not native-host proof.
Archive-source tests additionally cover wrong-owner construction, traversal/absolute filenames,
symlink and non-regular archive entries, foreign owner or non-private mode, over-reservation size,
single-operation reuse rejection, rename/symlink substitution after lookup, short/error reads,
descriptor cleanup on every exit, absent restoration without a read, and exact source/release/capture
identity plus digest and manifest verification before guest mutation. Contract tests pin the exact
`restore(tree, release, capture, source)` signature. Faults injected after writes begin prove a
read or close error stops Task 2 writes; pre-write rejection proves zero tree mutation. Contract
tests pin `restore(tree, release, capture, source)` and use a capability fake that exposes no path,
rename, phase, fsync-ordering, or restart operation. Task 3 crash tests prove it durably classifies
the retained owned partial before returning and never publishes a failed staging tree. Publication
tests inject loss immediately before/after each libguestfs move, guest sync, evidence fsync, rollback
move, and old-aside removal. They assert the exact three-name layouts and identity classification
above, cover present-empty and desired-absence paths, and prove live-name absence occurs only while
inactive. Boot, readiness, and lifecycle advancement remain blocked until fresh desired-state
verification plus durable `publication-complete` or `absence-complete`. Old-aside removal is
completion-evidence-gated. Tests also prove no guestmount, `renameat2`, appliance helper, or new host
prerequisite is used.
Migration tests additionally freeze inventory order and migration immutability, inspect the exact
replacement CHECK, accept canonical binding rows, and reject missing, legacy, scalar/array,
malformed-UUID, extra-key, cross-System, cross-Run, and cross-activation recovery points without
changing role grants. Upgrade fixtures prove a reachable legacy row aborts migration and that an
exact-definition but `NOT VALID` 0121 constraint is rejected with no partial DDL. Compatibility assertions cover both
sides of the first-binding-write boundary: pre-write application rollback and post-write
roll-forward-only recovery.
