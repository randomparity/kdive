# Remote root-boot provenance

## Scope

Issue #2106 implements the root-authority slice of
[ADR-0583](../../adr/0583-external-run-boot-uses-prepared-recovery-points.md). It records and
validates versioned root facts for remote-libvirt images and binds an accepted record to the
System that was provisioned from that image. Shared external-boot plans and `RootSpecV1` are owned
by #2115; provider activation, module restoration, and recovery remain owned by other siblings.
Implementation is blocked until #2115 lands.

## Design

`RootSpecV1` is the closed provider-neutral value supplied by #2115 from ADR-0583. This change
consumes it without defining a competing serializer or model. Validation admits only the
`stage-inspection`/`staged-image` and `catalog-attestation`/`catalog-image` pairs, lowercase
SHA-256 image identities, a matching architecture, one leading `root=` argument matching
`root`, and non-conflicting ordered storage arguments. Unknown fields or schema versions fail
closed.

Remote `virt-builder` output is inspected before publication through a dedicated bounded runner.
Each invocation retains the existing monotonic timeout and permits at most 1 MiB combined stdout
and stderr. The runner terminates the process and fails with `PROVISIONING_FAILURE` when time or
bytes are exceeded, when output is malformed, or when the inspected facts are incomplete; no image
is published, and the recovery action is to rebuild and retry. Inspection derives the root
filesystem UUID and type without mounting or executing guest code. The image digest is computed
before the resulting `stage-inspection` record is serialized, so the facts and identity describe
the same immutable bytes. Unlike advisory package-version capture, root authority cannot be
omitted from a newly built image.

Only mechanically verified provenance authorizes external boot. KDIVE-built images receive root
facts bound to the digest KDIVE computed over the complete image. Operator-staged images whose
bytes KDIVE has not mechanically verified remain eligible only for their existing disk/GRUB path;
operator attestation alone is insufficient and does not produce an authority record. The existing
typed `[image.attested]` capability fields remain readable but never authorize external boot.

Migration 0120 creates `system_root_provenance`, one immutable row per System. At System admission,
the server resolves the checksum-pinned remote `base_image_source` to exactly one visible registered
catalog row, rejects duplicate matches, validates its root value, and snapshots the catalog row ID,
image digest, root value, architecture, and Allocation project in the same transaction as the
System. The snapshot is immutable and does not follow later catalog mutation or deletion. A
requesting Run and Investigation are authorized later through their existing authoritative System,
Allocation, project, and Investigation relationships; the snapshot deliberately has no
Investigation column. A System without a checksum pin, or whose image lacks mechanically verified
root provenance, remains disk/GRUB-only. Callers never select an authority row.

## Failure behavior and compatibility

Malformed root facts are configuration errors that identify the invalid field and direct the
operator to rebuild/reinspect the image, while ordinary provisioning remains
available for legacy rows lacking root provenance. Existing database rows and inventory files need
no backfill. Migration 0120 is additive and forward-only.

## Security boundaries

The authority input is a guest image produced by `virt-builder`; operator-authored attestation is
not authority. Inspection uses fixed argv, a monotonic timeout, a 1 MiB combined-output cap,
defused parsing, one accepted root filesystem, and no guest execution. Validation rejects unknown
fields, controls, duplicate/conflicting arguments, architecture mismatches, and noncanonical
digests. Admission derives ownership from the locked Allocation, System request, and catalog row;
later Run admission uses existing authoritative relationships. No caller-provided authority
identifier is accepted. Diagnostics expose field names and recovery guidance, not image content,
paths, or raw inspection output.

## Tests

Unit tests consume #2115's closed root value and cover digest/authority pairing, conflicting facts,
and remote build serialization. Bounded-runner tests cover exactly 1 MiB, 1 MiB plus one byte,
timeout, malformed output, termination, `PROVISIONING_FAILURE`, and no publication. Database and
service tests cover immutable binding, same-project admission, stale image identity, architecture
mismatch, and Run/Investigation rejection through existing authoritative relationships. Legacy and
operator-staged unverified images prove disk/GRUB admission remains unchanged.
