# External Run-boot and recovery contract design

## Scope

Issue #2105 defines the epic contract decomposed into #2112, adopted issues #2106–#2110, and
implementation issues #2113–#2121. This change records the contract; it does not implement provider
ports, persistence, uploads, activation, reaping, or a bare-metal provider.
[ADR-0583](../../adr/0583-external-run-boot-uses-prepared-recovery-points.md) is the normative schema,
identity-vector, state-machine, and ordering definition incorporated into this specification without
variation. Issues #2113 and #2114 separately choose the fencing/quiescence and remote offline
restoration mechanisms; this specification states only their required outcomes.

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

The exact closed envelopes and golden vectors in ADR-0583 are normative and are incorporated here
without variation. Follow-up implementation must use every ADR field and identity rule and may split
modules without changing those meanings.

The shared runtime exposes six typed operations over the ADR-exact values: materialize the plan,
prepare recovery, activate, observe provider state, recover, and clean up. Preparation returns an
opaque recovery reference plus exact source and target state identities; observation returns one of
`source`, `target`, `owned-partial`, `conflict`, or `unreadable`. This specification deliberately does
not repeat shortened dataclass fields: the ADR's closed envelopes and golden vectors are the only data
model, so a consumer cannot conform to one document while failing the other.

`provider_ref` is an opaque, bounded, non-secret identifier. Core stores it and returns it only to
the same provider runtime; it does not parse, log, or expose it through MCP. A provider may resolve
the reference to provider-owned state, but the reference itself must not be a path, URL, credential,
or serialized XML.

Every SHA-256 value uses lowercase hexadecimal with a `sha256:` prefix. Identity uses the ADR's
canonical JSON and domain-separation rules and binds the ordered command line and all artifact
versions. UUIDs serialize as lowercase hyphenated strings.

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

External-build finalization extracts and persists the exact `boot/vmlinuz` digest and measured kernel
evidence; plan construction copies that trusted evidence. Materialization recomputes and requires an
exact match before publishing. The running-kernel
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

The complete rendered string is stored in `ExternalBootPlan.cmdline`; `debug_cmdline` preserves the
nullable caller extra and `platform_arguments` preserves the ordered platform-owned tokens. Providers
render `cmdline` without adding,
removing, or reordering tokens. A caller cannot supply raw root tokens through the Run argument
surface; a collision fails plan construction.

Root provenance authority is closed:

- `stage-inspection` with source kind `staged-image`: bounded verified inspection of the exact staged
  image emitted the record; or
- `catalog-attestation` with source kind `catalog-image`: a typed catalog attestation binds the root
  value, ordered root arguments, architecture, schema version, and operator declaration to the exact
  image identity. The current two-field attestation is insufficient and does not authorize external
  boot.

The closed `source` object contains only `kind` and immutable `identity`; both must match the System's
persisted base-image provenance.
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

`module-source-manifest-v1` first omits only absolute `build` and `source` symlinks at the release
root, matching the existing safe extraction filter; every other absolute or escaping link is
rejected. It then sorts relative UTF-8 paths by encoded bytes and rejects absolute paths, `.`/`..`,
duplicate normalized paths, hard links, devices, sockets, and FIFOs. Each directory, regular file,
or contained relative symlink records its
normalized path, type, normalized permission bits, and regular-file size/SHA-256 or symlink target;
uid, gid, and timestamps are excluded. This digest covers validated bundle input.

The source hash input is ASCII `kdive-module-source-manifest-v1`, NUL, then compact UTF-8 JSON
`{"entries":[...],"schema":"module-source-manifest-v1"}` with keys sorted by Unicode code point,
entries in encoded-path order, standard JSON escaping, no whitespace or trailing newline, JSON
integer sizes, four-character lowercase octal modes, and lowercase `sha256:<hex>` digests. Strings
must already be NFC; non-NFC and invalid UTF-8 are rejected. Empty input uses an empty entries array.

Source entries are exactly `{"mode":"0755","path":str,"type":"dir"}`;
`{"mode":"0644|0755","path":str,"sha256":"sha256:<64-hex>","size":int,"type":"file"}`;
or `{"mode":"0777","path":str,"target":str,"type":"symlink"}`. Installed entries add required
per-entry `gid:int`, `uid:int`, `xattrs:object`, and `xattrs_supported:bool`; no other key is allowed.
The xattrs object is empty when unsupported, otherwise its UTF-8 NFC names map to unpadded standard
base64. A non-UTF-8/non-NFC xattr name makes observation a third state.

Source metadata is normalized before staging to uid/gid `0`; `0755` directories; `0755` regular
files with any source execute bit and `0644` otherwise; and `0777` symlinks. Source ACLs and xattrs
are discarded. After the provider applies its configured label policy, `module-installed-tree-v1`
uses prefix `kdive-module-installed-tree-v1` and the same JSON rules, adding observed uid/gid and a
boolean `xattrs_supported` plus a sorted xattrs object with unpadded-base64 values. It includes POSIX
ACL and `security.*` xattrs; absence and unsupported-xattr filesystems are distinct values. Recovery
preserves and verifies this installed metadata.

Materialization stages the module tree, runs required indexing, and computes
`installed-module-tree-v1` with the same walker over the final tree. Generated files such as
`modules.dep` are included only there. The returned installed digest enters target provider-state
identity. Materialization does not change the System. Core commits `preparing`; the provider durably
records the exact source definition, prior power state, and prior
`/lib/modules/<kernel_release>` tree or its absence while proving the domain inactive. Only complete,
ownership-bound recovery evidence permits `prepared`. Reconciliation resumes or safely abandons an
interrupted preparation without recapturing a new baseline; abandonment restores source definition
and prior power state first. Activation
publishes the exact staged tree from `prepared`, then applies and boots the target definition.
Failure to quiesce leaves `preparing` and changes nothing. An exact tree may be
reused and a different same-release tree is replaced. Recovery performs the same offline boundary,
then restores the prior tree or removes the Run's tree when none existed. Local-libvirt adapts its
existing injection to this ordering. Remote-libvirt installs the same tree without rebuilding the
initrd. A provider unable to quiesce, stage, replace, verify, and restore it rejects before recovery
preparation.

Core commits `preparing` with reservation state `pending` before provider allocation. The provider
then reserves operator-configured `recovery_max_bytes` in its durable store for this System/Run
activation, and core records reservation state `ready` before materialization. Unit and scope are
bytes per activation; availability is read
at the response envelope's `server_time`. Exhaustion is retryable `CAPACITY_EXHAUSTED`, changes no
guest state, and directs cleanup of terminal artifacts or a cap increase before retry. Capture cannot
exceed the reservation; overrun restarts the source and fails `INSTALL_FAILURE`. Prepared, recovery,
and conflict states retain the reservation; abandonment, recovery, and teardown release it.

Remote artifacts use deterministic per-System/per-Run names in an operator-configured directory
pool. The provider resolves host paths internally after upload; no path crosses the shared seam.
Upload uses the existing bounded mutual-TLS libvirt stream pattern. Teardown and the reaper derive
ownership from System/Run identities plus KDIVE metadata, not filename alone.

## Activation, crash consistency, and recovery

Core persists one activation row per Run with the plan identity, opaque materialization and recovery
references, provider source/target state identities, state, attempt metadata, and last categorized
failure. The state transitions are:

```text
preparing -> prepared -> activating -> active
    |           |             |          |
    v           +-------------+----------+
abandoned                     v
                          recovering -> recovered
                              |
                              v
                       recovery_failed

preparing | prepared | activating | active | recovering -> recovery_conflict
recovery_conflict -> recovering
```

An active terminal Run enters `recovering` before cleanup when its System remains reusable. A
terminal `preparing` Run restores source definition and prior power state from its recovery evidence
before `abandoned`; a terminal `prepared` Run enters `recovering` and uses that evidence even while
provider state still equals the recorded source.
System teardown destroys the domain before cleanup instead of restoring it. Materialization and
recovery evidence cannot be removed before one of those ordered terminal paths completes. Illegal
transitions are programming errors. Operation attempts remain idempotent by Run and step, and all
transitions plus provider calls retain the existing per-System lock.

Ordering is strict:

1. Commit the activation row in `preparing` with reservation state `pending`.
2. Create the provider reservation and commit reservation state `ready`.
3. Materialize and verify the plan without changing the System.
4. Require provider-owned durable recovery evidence for the persistent
   definition, prior power state, prior module tree, and source/target identities while satisfying
   #2113's positive-quiescence outcome. #2114 chooses the remote capture/restore mechanism.
5. Commit `prepared` with the completed recovery reference and both state identities.
6. Commit `activating`.
7. Activate the module tree and persistent definition with compare-and-set against the recovery
   point's source state.
8. Observe the versioned persistent-definition projection plus module-tree state.
9. Prove fresh readiness, running-kernel identity, and the effective command line before committing
   externally usable `active`; persistent target equality alone never authorizes `active`.

A crash during materialization leaves owned state referenced by the pending activation and
reservation; reconciliation resumes or removes it deterministically. A crash after `prepared` is
recoverable from the row. For `activating`, reconciliation observes
both persistent definition and module-tree identities. Exact target resumes the remaining readiness,
kernel-identity, and command-line proofs before `active`; exact source
completes `recovered`; a mixed state composed only of recorded source and target components is an
activation-owned partial and moves to `recovering`. An absent, unreadable, or third component enters
`recovery_conflict` and preserves evidence for an operator; it is never overwritten. A failed restore
remains retryable only until its persisted recovery deadline and never declares the System ready.
Expiry transitions to terminal `recovery_failed`, retains the evidence and reservation, exposes
non-retryable `CONFLICT`, and permits only authorized System teardown.

Remote recovery records the exact persistent/inactive domain definition before external activation.
Definition identity version 1 splits that XML into a preserved digest and boot projection. The
preserved digest is canonical XML of the entire definition after removing only `/domain/os/kernel`,
`/domain/os/initrd`, and `/domain/os/cmdline`; aliases, addresses, devices, controllers, firmware,
storage attributes, and QEMU arguments remain. The boot projection is canonical JSON for those three
fields and distinguishes absent from empty. Defused parsing forbids DTDs/entities, removes
whitespace-only child-bearing `.text` and all whitespace-only `.tail`, rejects non-NFC character
data, then uses Python 3.14 Canonical XML 2.0 with comments off, text stripping off, and prefix
rewriting on. UTF-8 output has no newline and is hashed after `kdive-libvirt-preserved-v1` plus NUL.
Prepare
clones the observed source and changes only those fields; observation repeats the same split. Live
XML is excluded. The provider validates that the source belongs to the System and represents
disk/GRUB boot before storing it. Restore uses the recorded source with compare-and-set against
target or activation-owned partial state.

Remote prepare accepts a disk/GRUB source only when its inactive boot projection has no kernel,
initrd, or cmdline; KDIVE metadata binds it to the System; the sole boot disk uses the deterministic
System overlay, configured pool, expected target and bus; disk is the only boot device; and
loader/firmware/NVRAM match the provisioning profile. An external projection must be owned by a
matching durable activation row and recovered under the System lock before another prepare. Any
other mismatch or unowned external projection enters `recovery_conflict`.

Boot projection is compact sorted-key JSON with schema, kernel, initrd, and cmdline keys (the latter
three nullable), hashed after `kdive-libvirt-boot-projection-v1` plus NUL. The ADR's preserved-XML and
all-null projection digests are mandatory golden vectors.

When a reusable System recovers from `active`, the provider stops the domain, verifies it inactive,
restores the prior module tree and persistent definition, boots that definition, and proves both a
fresh boot and the existing System readiness contract before committing `recovered`. GRUB's selected
kernel is not derivable from inactive domain XML and is not an identity gate. Failure to reach
readiness before the persisted deadline transitions to `recovery_failed` with evidence and
reservation retained; retries before that deadline repeat the sequence. System teardown destroys
without restore/reboot. The record survives
until the ordered cleanup path completes, so configuration drift cannot rewrite the recovery target.

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
- Image provenance to plan construction: accept only `stage-inspection`/`staged-image` and
  `catalog-attestation`/`catalog-image`, matching the exact persisted source kind and immutable
  identity; reject unknown schema and architecture before provider work.
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
  ownership, architecture/release/root conflicts, JSON/XML golden vectors, and a test-only
  non-libvirt runtime.
- Archive tests cover duplicates, links, traversal, malformed headers, missing/multiple vmlinuz,
  wrong modules release, expansion bounds, deterministic `build`/`source` link omission,
  NFC rejection, canonical-byte test vectors, metadata normalization, ACL/xattr drift,
  generated-index installed identity, digest mismatch, and partial cleanup.
- State-machine and adversarial tests fault every boundary before/after provider calls and database
  commits, including worker loss during offline prepare, restoration of prior power state,
  capacity reservation/exhaustion/overrun, prepared abandonment, same-release module
  replacement/restoration, a running domain after worker loss, duplicate delivery, and concurrent
  retry under the System lock.
- Provider tests prove local behavior remains unchanged and remote upload, path resolution, XML
  preservation, full preserved-definition comparison across XML syntax normalization,
  GRUB-source admission and unowned-external conflict, compare-and-set activation, exact offline
  module restoration, recovered GRUB readiness, idempotent cleanup, and reaping.
- Remote `live_vm` boots the exact paired artifacts, verifies extracted-kernel identity, exercises a
  forced activation failure, restores GRUB, and proves the System remains usable.

No HTTP/iPXE schema test is required. The non-libvirt consumer proves only that shared values and
ports contain no libvirt-specific type or locator.
