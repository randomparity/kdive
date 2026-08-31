# Local-libvirt external-boot implementation plan

## Goal and architecture

Implement issue #2108's local-libvirt `ExternalBootPorts` primitives over the accepted ADR-0583 and
ADR-0586 contracts. A closed activation binding and recovery point carry ownership; local recovery
uses an atomically published provider-host directory plus a narrow libguestfs seam. Authority proof
translation, orchestration, capacity release, and advertisement remain #2140/#2118.
The operator's 2026-08-30 Task 2 decision requires a symmetric owner-bound archive source; the
guest writer receives that capability and the closed capture descriptor, never a host path.
The follow-up boundary selected that day keeps Task 2 as pure validated tree I/O over a
Task-3-provided authenticated guest-tree capability. Task 3 owns persistent staging names, durable
phase evidence, publication, and restart classification.

Tech stack: Python 3.14, Pydantic closed values, libvirt-python, libguestfs, stdlib tar/XML/hash/fsync,
pytest, Ruff, ty, and prek.

## Global constraints

- Targets are x86_64 and ppc64le; do not infer target behavior from the x86_64 development host.
- Add no dependency. Migration 0124 may only replace the activation-ledger recovery-point ownership
  CHECK; keep `LocalLibvirtInstall` behavior unchanged.
- Recovery bounds are 200,000 entries and 8,589,934,592 uncompressed regular-file bytes.
- Never follow archive, recovery-root, or guest-tree symlinks. Preserve recovery symlink targets
  verbatim, including absolute targets. Reject hard links, special files, non-NFC/undecodable names,
  and noncanonical entry paths.
- Provider paths, XML, cmdline, guest names/content, credentials, and raw output never enter refs,
  exceptions, or logs.
- `AuthorityMutationAdapter`, authority proof construction, terminal replay, capacity orchestration,
  and advertisement remain #2140/#2118. Do not compose the new local port into production runtime.
- Verification commands: focused `just test-verbose <path>`, then `just lint`, `just type`,
  `git diff --check`, staged `prek run`; pre-push `just ci` belongs to delivery.

## File map

- Modify `src/kdive/providers/ports/external_boot.py`: activation binding, recovery-point schema, and
  prepare signature.
- Modify `src/kdive/domain/external_boot_activation.py` and
  `src/kdive/db/external_boot_activations.py`: consume `RecoveryPoint.binding` consistently.
- Modify `src/kdive/providers/fault_inject/lifecycle/external_boot.py`: pre-release contract parity.
- Modify `tests/providers/ports/test_external_boot.py`: canonical values and consumer conformance.
- Modify `tests/domain/test_external_boot_activation.py` and
  `tests/db/test_external_boot_activation_repository.py`: binding validation and persistence.
- Create `src/kdive/providers/local_libvirt/lifecycle/boot/recovery.py`: bounded manifest and narrow
  guestfs capture/observe/install/restore seam.
- Create `src/kdive/providers/local_libvirt/lifecycle/external_boot.py`: local six-port orchestration,
  recovery directory, XML identity, phases, cleanup, and finalization primitive.
- Modify `src/kdive/providers/local_libvirt/lifecycle/install.py` only to expose an existing helper
  when exact behavior is reused; do not alter legacy facade behavior.
- Create `tests/providers/local_libvirt/lifecycle/boot/test_recovery.py` and
  `tests/providers/local_libvirt/test_external_boot.py`.
- Modify `tests/providers/local_libvirt/test_composition.py`: prove no advertisement/composition.
- Create `tests/adversarial/test_local_external_boot_recovery.py` for crash/retry matrices.
- Create `src/kdive/db/schema/0124_external_boot_activation_binding.sql` and update exact migration
  inventory tests in `tests/db/test_migrate.py`; do not edit migration 0121.
- Create `tests/db/test_external_boot_activation_binding_migration.py`: exact replacement-CHECK,
  compatibility-preflight, atomic-abort, grants, and persistence proofs for migration 0124.

## Task 1: Close activation ownership across the shared contract

**Files:** modify the shared port, fault-inject implementation, domain activation model, activation
repository, and their shared/domain/database tests named above.

**Interfaces:** add exactly:

```python
class ExternalBootActivationBinding(_ClosedValue):
    system_id: CanonicalUuid
    run_id: CanonicalUuid
    activation_id: CanonicalUuid


class RecoveryPoint(_ClosedValue):
    schema_: Literal["external-boot-recovery-v1"] = Field(
        "external-boot-recovery-v1", alias="schema"
    )
    binding: ExternalBootActivationBinding
    plan_identity: Digest
    materialization_identity: Digest
    recovery_ref: OpaqueProviderRef
    source_state: ProviderStateIdentity
    target_state: ProviderStateIdentity


def prepare(
    self,
    materialization: ExternalBootMaterialization,
    binding: ExternalBootActivationBinding,
    authority: OpaqueProviderRef,
) -> RecoveryPoint: ...
```

1. Add tests proving canonical JSON round-trip, missing/extra-field rejection, System/Run mismatch
   rejection at prepare, activation changes produce unequal points, legacy `ownership` is rejected,
   and fault-inject implements the revised protocol. Run the exact test under a controlled fault
   that skips the System comparison; expect the mismatch test to fail, then restore.
2. Implement the values/signature and update fault-inject to copy the exact binding. Replace
   `recovery_point.ownership` reads in the domain invariant and repository serializer with
   `recovery_point.binding`; preserve their existing System/Run equality checks and add activation
   round-trip assertions. Repository-search `prepare(`, `.ownership`, and `RecoveryPoint(` and update
   every direct in-tree caller; discovery of an external/versioned caller stops at scope checkpoint.
   Add migration 0124 with a same-transaction compatibility preflight before any
   `DROP CONSTRAINT`. Require the exact normalized 0121 definition with `convalidated=true`, then
   abort on a reachable existing recovery point with legacy ownership. Only after it passes, drop and recreate
   `external_boot_activation_evidence_ownership`, preserving every non-recovery arm. Require
   `jsonb_typeof(binding) = 'object'`, the exact keyset `{system_id, run_id, activation_id}`,
   canonical UUID casts, and equality of all three values to the ledger System, Run, and activation.
   Perform no data rewrite and add no table, role, or grant changes.

   In `tests/db/test_external_boot_activation_binding_migration.py`, prove the exact CHECK shape,
   canonical persistence and each malformed/legacy/cross-owner row rejection after migration. A
   validated-0121 legacy-row fixture and an exact-definition `NOT VALID` fixture must prove the
   migration transaction aborts before the drop and leaves the old CHECK installed with no partial
   DDL. Other malformed binding shapes are unreachable under validated 0121 and need no fabricated
   pre-migration fixture. Prove the role/grant inventory is unchanged. In `tests/db/test_migrate.py`, add 0124 to the
   exact ordered inventory and immutable historical-prefix assertions without changing 0121.
   Run `just test-verbose tests/db/test_external_boot_activation_binding_migration.py` and
   `just test-verbose tests/db/test_migrate.py`; expect all passed. Application rollback is allowed
   only before the first binding-shaped row is written. After that boundary, recover by roll-forward;
   never restore the removed wire shape.
3. Run `just test-verbose tests/providers/ports/test_external_boot.py`,
   `just test-verbose tests/domain/test_external_boot_activation.py`, and
   `just test-verbose tests/db/test_external_boot_activation_repository.py`; expect all passed.
   Run `just lint`, `just type`, and `git diff --check`; expect exit 0. Commit explicit Task 1 paths as
   `refactor(providers): bind external boot to activation`.

**Acceptance:** one owner representation exists; every later operation can receive exact activation
ownership; no production composition changes.

## Task 2: Implement exact local module capture and restoration

**Files:** create `recovery.py` and `test_recovery.py`.

**Interfaces:** define frozen `ModuleArchiveCapture` and `AbsentModuleCapture`, union
`ModuleCapture`, and:

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


class RealGuestRecoveryWriter:
    # implements pure tree I/O through the supplied authenticated capability
    ...
```

Task 3 constructs and owns `AuthenticatedGuestTree` after authenticating System ownership,
verifying the domain inactive, opening the owned overlay, and binding either the live release tree
for read-only capture/observation or a private staging tree for install/restore. Before passing a
mutable staging capability, Task 3 writes and fsyncs intent binding its deterministic staging name,
System, activation, release, operation, and expected identity. Its Task 2 surface
contains only relative inspection/mutation operations. It exposes no overlay or guest path, staging
name, live rename/publication, durable phase write, fsync-ordering decision, or restart classifier.
Task 2 creates and recognizes no persistent `.kdive-partial`, `.kdive-previous`, or equivalent
namespace. Task 3 closes the capability and decides staging cleanup after Task 2 returns.

`RecoveryArchiveSink` and `RecoveryArchiveSource` are injected owner-bound capabilities constructed
by `LocalLibvirtExternalBoot` after resolving and authenticating the recovery token beneath its
configured root. The sink exposes only exclusive staged archive creation and fsync. The source
opens only the validated capture's relative archive filename beneath the retained owner directory,
using no-follow regular-file/service-owner/private-mode checks and a bound no larger than the owned
recovery reservation. One source owns its authenticated directory descriptor for one restore call,
retains the opened archive descriptor against substitution, and rejects reuse. It exposes a bounded
read-only stream and closes file and directory descriptors on every exit. Neither accepts or reveals
a caller-selected path; `GuestRecoveryWriter` cannot resolve paths or choose a source or destination.
Absent restoration never opens the source.

1. Write golden tests for empty, regular-file, unsupported-xattr, ACL/security-xattr, and absolute
   `build` symlink manifests. Assert the empty digest is
   `sha256:7048c9e065ecf77a964188f42aaebb79a3e8238ecc47736ae47239b8ceec30a5`.
   Add rejection tests for traversal entry paths, hard links, devices/FIFOs/sockets, undecodable or
   non-NFC names/targets, duplicates, count/byte overflow, and xattr read failure after support.
2. Implement the ADR-0583 canonical walker: lstat/no-follow, UTF-8 byte path ordering, exact compact
   JSON escaping, modes, uid/gid, unpadded standard base64 xattrs, domain-separated SHA-256, and
   explicit absence. Use directory-relative guestfs operations; accept no caller destination path.
3. Add capture/install/restore tests proving source/target manifest verification, exact absent
   restoration, stopped mutation after failure, and that symlink targets are copied but never
   followed. Cover wrong-owner source construction, absolute/traversal capture
   names, archive symlink/non-regular/foreign-owner/non-private/over-reservation rejection, bounded
   reads, absent restore without source access, source reuse rejection after success or failure, and
   descriptor cleanup on every exit. Contract tests pin
   `restore(tree, release, capture, source)`. Validate lookup, owner, release, capture, metadata, and
   bounds before writes and assert each rejection causes zero tree mutation. After writes begin,
   inject short/error reads and close faults and assert Task 2 stops mutation. Use a capability fake
   with no overlay/guest path, staging-name, rename/publication, phase-write, fsync-ordering, or
   restart-classification method; assert Task 2 never creates `.kdive-partial`, `.kdive-previous`,
   or an equivalent persistent name. Inject a manifest comparator fault and observe red before
   restoring.
4. Run `just test-verbose tests/providers/local_libvirt/lifecycle/boot/test_recovery.py`; expect all
   tests passed. Run lint/type/diff checks and commit as
   `feat(local-libvirt): preserve external boot modules`.

**Acceptance:** capture and observation produce byte-identical recovery identities; restoration
recreates exact metadata or absence; hostile topology cannot escape the release directory.

## Task 3: Implement the local six-port recovery state machine

**Files:** create `external_boot.py` and `test_external_boot.py`; narrowly expose exact existing
install/XML/readiness helpers only when required.

**Interfaces:** define closed `LocalRecoveryMetadataV1`, `FinalizeCleanupProof`, injected
`LocalExternalBootIO` seams, and:

```python
class LocalLibvirtExternalBoot(ExternalBootPorts):
    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef
    ) -> ExternalBootMaterialization: ...
    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> RecoveryPoint: ...
    def activate(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None: ...
    def observe(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> RunningKernelObservation: ...
    def recover(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None: ...
    def cleanup(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None: ...
    def finalize_cleanup_tombstone(
        self, recovery: RecoveryPoint, proof: FinalizeCleanupProof, authority: OpaqueProviderRef
    ) -> None: ...
```

1. Add XML golden-vector tests from ADR-0583, preserved-subtree/device/QEMU argument retention,
   malformed/DTD/entity/non-NFC rejection, and optional-initrd projections.
2. Add recovery-root tests for exact relative token parsing, owner-only modes, no-follow exclusive
   writes, file/directory/parent fsync ordering, atomic rename, complete-point reopening, cross-field
   substitution, and quarantine. Test the mkdir-before-intent empty-partial rule separately.
3. Before implementation, add the minimal crash tests for loss immediately before/after intent
   fsync, stop, module publish, XML define, source restoration, tombstone publish, and tombstone
   delete. Include initially-running source → inactive module/XML restoration → durable
   `source-restored` → boot/readiness → cleanup, and assert cleanup never calls guestfs or live
   composite observation. Assert fresh-instance retry outcomes and before/after snapshots. Run them
   now and require failures attributable to the absent state machine, not fixture errors.
4. Implement materialization by reusing streaming bundle extraction and staged writes. Verify every
   digest/obligation; publish only complete deterministic System/Run-owned artifacts.
5. Implement prepare ordering: read initial power/XML, write and fsync `pre-stop-intent`, stop and
   verify inactive, construct the authenticated live-tree capability, capture modules, render target,
   and publish the complete recovery directory. Retry uses intent rather than re-deriving power/XML
   and restores source on failure when evidence permits.
6. Implement private inactive `_observe_composite`; Task 3 creates the deterministic staging tree,
   writes and fsyncs its fully bound intent, constructs the private staging-tree capability, and only
   then passes it to Task 2 install/restore. On Task 2 failure it records the observed owned partial
   phase before returning and never publishes it; a crash inside Task 2 remains restart-classifiable
   from the pre-call intent. On success it independently verifies the
   staged manifest, fsyncs phase evidence, performs the same-filesystem live-tree rename, and fsyncs
   the containing directory. Activate modules then XML with durable phases. Recover modules then
   exact XML, verify their complete source composite while inactive, and fsync a `source-restored`
   phase before restoring prior power/readiness. At every entry reopen metadata
   and compare the entire point. Public observe uses durable target-defined evidence plus existing
   running-kernel readiness only and never opens a live overlay. Cleanup after `recovered`
   authenticates complete point equality and `source-restored`; it performs no fresh guestfs/live
   composite observation even when the restored source is running.
7. Implement cleanup as payload deletion plus authenticated accounted tombstone. Implement U1a
   finalization: present tombstone requires exact proof equality; absent success requires the exact
   current-binding, same-operation `mutation-started` proof supplied by #2140. Reject every stale,
   cross-binding, cross-operation, or unjournaled proof. Do not release capacity or set core state.
8. Run `just test-verbose tests/providers/local_libvirt/test_external_boot.py`; expect all passed.
   Run the legacy install file to prove behavior preservation. Run lint/type/diff and commit as
   `feat(local-libvirt): implement external boot ports`.

**Acceptance:** every source/target/owned-partial state resumes deterministically; every third,
unreadable, or cross-owner state makes zero writes; exact recovery and cleanup are retryable.

## Task 4: Prove crash ordering and preserve non-advertisement

**Files:** create the adversarial test and modify composition test only.

**Interfaces:** consume Task 3 public methods and injected IO. Do not add runtime composition.

1. Expand Task 3's biting fault tests across every lost process/response point before and after intent fsync, stop, capture publication,
   module rename, XML define, restoration writes, tombstone publication, tombstone deletion, and
   parent fsync. Restart a fresh instance from disk and assert exact continuation or conflict.
2. Race/replay two bindings across same System/Run with different activation IDs; substitute every
   RecoveryPoint field and U1a proof field; assert before/after filesystem/XML snapshots are equal.
3. Model U1a windows: unresolved exact mutation-started after delete succeeds; stale/current-binding
   mismatch fails; terminal replay is recorded as a #2140 contract test requirement and never calls
   local finalization. A fake handoff records that local finalization must precede reservation release
   and cleanup completion; it does not claim to prove excluded DB/core orchestration. End-to-end
   ordering remains required acceptance for #2140/#2118.
4. Assert production `ProviderRuntime` and composition still do not expose external boot. This is a
   required negative proof, not deferred implementation.
5. Run focused local external-boot, shared-port, legacy-install, composition, and adversarial paths;
   expect all passed. Perform controlled owner-comparator fault and observe red. Run `just lint`,
   `just type`, `git diff --check`, and staged `prek run`; expect exit 0. Commit as
   `test(local-libvirt): prove external boot recovery`.

**Acceptance:** crash/retry and cross-owner matrices pass; legacy install remains green; no production
advertisement or authority adapter exists. Local tests prove the bounded handoff contract only;
#2140/#2118 own the end-to-end capacity-release/cleanup-completion proof.

## Final verification and handoff

Run the complete focused set, `just lint`, `just type`, `prek run`, and `git diff --check`. Then run
whole-branch trial-loop, security review, simplification, and bare `just ci` under the parent quest.
The manually dispatched `live_vm` tier is not available proof unless an operator supplies its host;
report it as not run rather than skipped success. Delivery must not advertise external boot and must
record #2140's terminal replay/finalization plus #2118's release/cleanup ordering as prerequisites.
