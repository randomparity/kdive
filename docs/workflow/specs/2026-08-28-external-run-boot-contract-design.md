# External Run-boot and recovery contract design

## Scope

Issue #2105 defines the design contract used by follow-up issues #2106–#2110. This change records
the contract; it does not implement provider ports, persistence, uploads, activation, reaping, or a
bare-metal provider. [ADR-0583](../../adr/0583-external-run-boot-uses-prepared-recovery-points.md)
settles recovery ownership and crash consistency.

The design replaces the unpublished System-profile INITRD direction in closed PR #2104. Initial
remote System provisioning continues to boot the operator-provided disk image through GRUB. An
iterative Run may use external boot only after its image has accepted root provenance and its
external build has finalized one paired artifact set.

## Goals and acceptance

The implementation is complete when:

1. One provider-neutral plan represents the same finalized kernel bundle, optional initrd, root
   specification, and ordered command line for local-libvirt, remote-libvirt, and a test-only
   non-libvirt consumer.
2. The plan binds the optional initrd to the kernel build and binds the running-kernel proof to the
   SHA-256 of extracted `boot/vmlinuz`, not only to an object key or bundle digest.
3. Root provenance is versioned, authority-bearing, immutable, architecture-compatible, and
   validated before external boot; a missing or invalid record keeps the System on GRUB and returns
   an actionable configuration error.
4. Materialization, recovery-point preparation, activation, recovery, and cleanup are provider
   operations whose shared inputs and outputs expose no libvirt type, path, volume, URL, or iPXE
   concept.
5. A worker loss at every provider/database boundary converges to either the exact desired external
   boot or the exact recorded disk/GRUB recovery point, with no unowned artifact.
6. Local-libvirt retains its current atomic staging, module injection, optional-initrd handling,
   direct-kernel XML, retries, and teardown behavior.
7. Remote-libvirt uses mutually authenticated libvirt streams to publish per-System/per-Run boot
   artifacts, preserves the existing disk and devices during direct-kernel activation, and can
   restore the recorded GRUB definition without rebuilding the System.
8. Unit, contract, adversarial retry/cleanup, and remote `live_vm` tests prove artifact identity,
   recovery, and exact running-kernel identity on x86_64 and ppc64le where the provider is supported.

## Contract model

The names below are normative. Follow-up implementation may split their modules without changing
their fields or meanings.

```python
@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    object_ref: str
    version_id: str
    sha256: str

@dataclass(frozen=True, slots=True)
class RootProvenanceV1:
    authority: Literal["build-verified", "stage-inspected", "catalog-attested"]
    source_identity: str
    source_version: str

@dataclass(frozen=True, slots=True)
class RootSpecV1:
    schema_version: Literal[1]
    arch: str
    root: str
    arguments: tuple[str, ...]
    provenance: RootProvenanceV1

@dataclass(frozen=True, slots=True)
class ModuleInstallObligation:
    mode: Literal["system-root-tree"]
    kernel_release: str
    modules_tree_sha256: str

@dataclass(frozen=True, slots=True)
class ExternalBootPlan:
    schema_version: Literal[1]
    system_id: UUID
    run_id: UUID
    build_id: UUID
    arch: str
    kernel_release: str
    kernel_bundle: ArtifactIdentity
    initrd: ArtifactIdentity | None
    root: RootSpecV1
    command_line: tuple[str, ...]
    modules: ModuleInstallObligation
    identity: str

@dataclass(frozen=True, slots=True)
class BootMaterialization:
    plan_identity: str
    provider_ref: str
    extracted_kernel_sha256: str

@dataclass(frozen=True, slots=True)
class BootRecoveryPoint:
    plan_identity: str
    provider_ref: str
    source_state_identity: str
    target_state_identity: str

@dataclass(frozen=True, slots=True)
class BootObservedState:
    status: Literal["source", "target", "owned-partial", "conflict", "unreadable"]

class ExternalBootRuntime(Protocol):
    def materialize(self, plan: ExternalBootPlan) -> BootMaterialization: ...
    def prepare_recovery(
        self, plan: ExternalBootPlan, materialization: BootMaterialization
    ) -> BootRecoveryPoint: ...
    def activate(
        self,
        plan: ExternalBootPlan,
        materialization: BootMaterialization,
        recovery: BootRecoveryPoint,
    ) -> None: ...
    def observe_state(
        self, system_id: UUID, recovery: BootRecoveryPoint
    ) -> BootObservedState: ...
    def recover(self, system_id: UUID, recovery: BootRecoveryPoint) -> None: ...
    def cleanup(
        self,
        plan: ExternalBootPlan,
        materialization: BootMaterialization,
        recovery: BootRecoveryPoint,
    ) -> None: ...
```

`provider_ref` is an opaque, bounded, non-secret identifier. Core stores it and returns it only to
the same provider runtime; it does not parse, log, or expose it through MCP. A provider may resolve
the reference to provider-owned state, but the reference itself must not be a path, URL, credential,
or serialized XML.

Every SHA-256 value uses lowercase hexadecimal with a `sha256:` prefix. `identity` is the SHA-256 of
canonical JSON containing every preceding `ExternalBootPlan` field except `identity`, using sorted
object keys, UTF-8, no insignificant whitespace, and array order preserved. It therefore binds the
ordered command line and all artifact versions. UUIDs serialize as lowercase hyphenated strings.

## Paired artifacts and build ownership

`runs.complete_build` remains the only caller entry point. It may finalize no initrd or exactly one
initrd belonging to the same Run/build attempt as the kernel bundle. Finalization records immutable
object versions and content digests for both. Reusable-build adoption copies the complete set and
its original build identity; it never selects artifacts independently.

Plan construction rejects:

- an initrd without the finalized kernel bundle or with another build identity;
- a missing object version or digest;
- an architecture or kernel release mismatch between manifest, bundle inspection, and System;
- a bundle without exactly one regular `boot/vmlinuz` and one `lib/modules/<kernel_release>/`
  subtree, including duplicate archive members, links, traversal, or an expanded-size violation;
- a modules-tree digest that differs from the finalized manifest; and
- a plan identity that does not recompute from the canonical fields.

The extracted `boot/vmlinuz` digest is a materialization result because it identifies derived bytes.
The provider compares it with the finalized bundle manifest before publishing. The running-kernel
proof compares the guest-visible build identity or version plus the measured boot artifact against
that digest through the provider's live proof; a changed `boot_id` alone is readiness, not identity.

## Root specification and command-line ownership

`RootSpecV1.root` is the value after one `root=` token. It is non-empty UTF-8 without NUL or ASCII
control characters. `arguments` is the ordered, already-tokenized set of additional root/storage
arguments required by that image, such as `rootfstype=`, `rootflags=`, `rd.luks.uuid=`, or
`rd.lvm.lv=`. It contains no `root=` token and no capture/Run argument. Duplicate singleton keys or
conflicting `ro`/`rw` values are invalid.

The final command line is composed once in core in this order:

1. `root=<RootSpecV1.root>`;
2. `RootSpecV1.arguments` in recorded order;
3. the existing platform-independent Run and capture arguments.

The final tuple is stored in `ExternalBootPlan.command_line`. Providers render it without adding,
removing, or reordering tokens. A caller cannot supply raw root tokens through the Run argument
surface; a collision fails plan construction.

Root provenance authority is closed:

- `build-verified`: the KDIVE rootfs build measured the booted image layout and emitted the record;
- `stage-inspected`: a bounded, verified inspection of the exact staged image emitted it; or
- `catalog-attested`: a typed extension to the existing catalog attestation binds the root value,
  ordered root arguments, architecture, schema version, and operator declaration to the exact image
  digest/version. The current two-field attestation is insufficient and does not authorize external
  boot.

`source_identity` and `source_version` must match the System's persisted base-image provenance.
Unknown schema or authority values, a mismatch, or absent provenance yields
`CONFIGURATION_ERROR` before materialization, naming the invalid fact and the recovery action:
reinspect/rebuild the image or use the existing GRUB boot path. Pre-schema images are not backfilled
from a live guest during Run boot.

## Materialization and ownership

Materialization is deterministic for `(provider, System, Run, plan identity)` and idempotent under
the per-System advisory lock. A provider writes partials under a deterministic owned prefix, verifies
content, and publishes the final reference atomically. A retry reuses only a final materialization
whose plan identity and extracted-kernel digest match; any mismatch is `INSTALL_FAILURE` and leaves
the existing object untouched for investigation.

Materialization stages the module tree but does not change the System. Recovery preparation records
the prior `/lib/modules/<kernel_release>` tree or its absence. Activation atomically publishes the
exact staged tree before booting; an exact tree may be reused and a different same-release tree is
replaced. Recovery restores the prior tree or removes the Run's tree when none existed. Local-libvirt
adapts its existing injection to this ordering. Remote-libvirt installs the same tree without
rebuilding the initrd. A provider unable to stage, replace, verify, and restore it rejects before
recovery preparation.

Remote artifacts use deterministic per-System/per-Run names in an operator-configured directory
pool. The provider resolves host paths internally after upload; no path crosses the shared seam.
Upload uses the existing bounded mutual-TLS libvirt stream pattern. Teardown and the reaper derive
ownership from System/Run identities plus KDIVE metadata, not filename alone.

## Activation, crash consistency, and recovery

Core persists one activation row per Run with the plan identity, opaque materialization and recovery
references, provider source/target state identities, state, attempt metadata, and last categorized
failure. The state transitions are:

```text
prepared -> activating -> active
    |             |          |
    v             v          v
abandoned     recovering <---+
                  |
                  v
              recovered

activating | active | recovering -> recovery_conflict
```

An active terminal Run enters `recovering` before cleanup when its System remains reusable. A
terminal `prepared` Run enters `abandoned` only while provider state still equals the recorded source.
System teardown destroys the domain before cleanup instead of restoring it. Materialization and
recovery evidence cannot be removed before one of those ordered terminal paths completes. Illegal
transitions are programming errors. Operation attempts remain idempotent by Run and step, and all
transitions plus provider calls retain the existing per-System lock.

Ordering is strict:

1. Materialize and verify the plan.
2. Ask the provider to durably record the exact persistent boot definition and prior module tree,
   render the target persistent definition, and compute canonical source/target state identities.
3. Commit `prepared` with both opaque references and both state identities.
4. Commit `activating`.
5. Activate the module tree and persistent definition with compare-and-set against the recovery
   point's source state.
6. Observe the versioned persistent-definition projection plus module-tree state and commit `active`
   only on an exact target-state match.
7. Run readiness, then the separate running-kernel identity proof.

A crash before step 3 leaves only provider-owned unreferenced state, which the deterministic reaper
removes. A crash after step 3 is recoverable from the row. For `activating`, reconciliation observes
both persistent definition and module-tree identities. Exact target completes `active`; exact source
completes `recovered`; a mixed state composed only of recorded source and target components is an
activation-owned partial and moves to `recovering`. An absent, unreadable, or third component enters
`recovery_conflict` and preserves evidence for an operator; it is never overwritten. A failed restore
remains retryable in `recovering` and never declares the System ready.

Remote recovery records the exact persistent/inactive domain definition before external activation.
Definition identity version 1 is canonical JSON over domain identity, machine/CPU/memory/vCPU, boot
mode and kernel/initrd/cmdline, all disk and network identities, serial/console/guest-agent devices,
gdbstub and SSH-forward arguments, and KDIVE metadata. Keys and devices use stable semantic order;
argument order is retained. XML formatting, namespace prefixes, libvirt-added aliases/addresses, and
runtime/default expansion are excluded. The renderer emits this projection directly and observation
parses inactive XML into it; a field-set change requires a new version. Live XML is excluded. The
provider validates that the source belongs to the System and represents disk/GRUB boot before
storing it. Restore uses the recorded source with compare-and-set against target or activation-owned
partial state.

When a reusable System recovers from `active`, the provider stops the domain, verifies it inactive,
restores the prior module tree and persistent definition, boots that definition, and proves both a
fresh boot and the recorded baseline-kernel identity before committing `recovered`. A retry repeats
the sequence. System teardown destroys without restore/reboot. The record survives until the ordered
cleanup path completes, so configuration drift cannot rewrite the recovery target.

## Failure taxonomy

- Malformed, conflicting, unsupported, stale, or architecture-incompatible plan/root provenance:
  `CONFIGURATION_ERROR`, with the field and recovery action.
- Missing or version-changed object: `STALE_HANDLE` when worker-observable; a remote fetch/upload
  failure that cannot distinguish absence uses the existing bounded retry category.
- Invalid archive, digest, release, pairing, module tree, or materialization mismatch:
  `INSTALL_FAILURE`.
- Object-store or provider-control-plane fault: `INFRASTRUCTURE_FAILURE`; remote connection faults
  remain `TRANSPORT_FAILURE`.
- Guest never reaches readiness: `BOOT_TIMEOUT`; reachable guest with failed readiness or kernel
  identity proof: `READINESS_FAILURE`.

Every failure records references and bounded diagnostics, never artifact bytes, presigned URLs,
provider paths, XML, or unredacted transcripts.

## Security and trust boundaries

### Actors

Authenticated tenants control external-build uploads and allowed Run arguments. Operators control
catalog attestations, provider configuration, remote hosts, and storage pools. The object store and
remote libvirt endpoint are trusted services reached with scoped credentials; either may fail or
return stale data. One tenant must not select or observe another tenant's artifacts or provider
state.

### Boundaries and controls

- Tenant upload to build finalization: existing Run/build ownership, size limits, archive-member
  validation, immutable object versions, digests, and retention apply. Pairing is checked against
  the same finalized build identity.
- Image provenance to plan construction: accept only the three authority classes and exact persisted
  source identity/version; reject unknown schema and architecture before provider work.
- Run arguments to kernel command line: tokenize in core, reject root-key collisions and control
  characters, and pass argument arrays rather than shell text.
- Object store to worker/provider: use immutable versions, bounded streaming and extraction, digest
  verification, registered presigned capabilities, and mandatory redaction for their lifetime.
- Core to provider: opaque references are tenant/System/Run/plan-bound, size-bounded, non-secret,
  and never returned through MCP. Providers reject cross-owner or mismatched references.
- Worker to remote libvirt: existing mutual TLS, URI validation, timeouts, size bounds, and provider
  configuration apply. Remote paths and XML remain internal and are removed from errors.
- Provider state to reconciliation: canonical persistent definition and module identities are
  compared with both persisted states; owned partials recover, while absence, ambiguity, or a third
  identity enters conflict and is never overwritten.

This design does not protect a host administrator from the host they control, make uploaded kernels
safe to execute, introduce stronger tenant sandboxing, or define bare-metal network-boot security.
Those are existing deployment trust or excluded provider concerns.

## Verification

- Pure contract tests cover canonical identity, ordering sensitivity, optional initrd pairing,
  ownership, architecture/release/root conflicts, and a test-only non-libvirt runtime.
- Archive tests cover duplicates, links, traversal, malformed headers, missing/multiple vmlinuz,
  wrong modules release, expansion bounds, digest mismatch, and partial cleanup.
- State-machine and adversarial tests fault every boundary before/after provider calls and database
  commits, including prepared abandonment, same-release module replacement/restoration, a running
  domain after worker loss, duplicate delivery, and concurrent retry under the System lock.
- Provider tests prove local behavior remains unchanged and remote upload, path resolution, XML
  preservation, semantic projection across libvirt normalization, compare-and-set activation, exact
  offline module restoration, recovered-baseline boot proof, idempotent cleanup, and reaping.
- Remote `live_vm` boots the exact paired artifacts, verifies extracted-kernel identity, exercises a
  forced activation failure, restores GRUB, and proves the System remains usable.

No HTTP/iPXE schema test is required. The non-libvirt consumer proves only that shared values and
ports contain no libvirt-specific type or locator.
