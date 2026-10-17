# Verified remote staged roots

Issue #3218; ADR [0776](../../adr/0776-verified-remote-staging.md).
Operator approved the exact campaign design on 2026-10-16 (SHA-256
`d94de308ce0f426d1f3db3158c6f60cb3b207c1b3bfbb31c526f737debe09418`, section #3218).

## Scope and design

Extend the existing provider-neutral stage-volume orchestration and provider-owned wiring;
no ownership move is needed. Resolve the public config-managed staged row first. Copy the
local qcow2 into a private temporary directory using bounded reads, validate its self-contained
qcow2 header, then hash in bounded chunks. Accept versions 2/3; reject backing references,
external data-file features and unknown incompatible features before invoking inspection.
The bounded header check follows the [QEMU format](https://www.qemu.org/docs/master/interop/qcow2.html).
Reuse inspect_root_boot(snapshot, architecture, digest). Capture optional config from that
same snapshot, upload that snapshot to a fresh volume, then persist digest and root_spec
atomically on the still-matching public config row. The row identity, volume, state and
publication-attempt snapshot fence persistence; no metadata is committed for a repointed,
removed, private or pending row. Validate the inspector result's architecture, authority,
source kind and source identity before upload. Remove the private snapshot on exit.

Stage-volume requests fresh-only upload. Existing volume lookup is an actionable refusal;
a duplicate create race fails without reusing/deleting that volume. Other upload callers keep
the existing skip behavior. Inspection/upload/mandatory metadata failure is fatal. Config
capture/attach remains advisory. A post-upload DB failure leaves unverified remote residue;
report failure and require operator inspection/cleanup or a fresh target, never retry attestation
against an existing volume. No automatic remote overwrite or deletion is added.

Reconcile retains verified root facts/digest while the staged volume declaration is unchanged.
Repointing clears root_spec and digest; ordinary staged rows remain unverified. Reconcile
remains metadata-only. Catalog facts attest stage-time bytes, not subsequent remote replacement.
The separately installed authority private-base digest/file-identity checks remain unchanged.

## Success

- Stage-volume's successful remote upload and committed root identity describe identical bytes.
- Missing/invalid inspection and failed fresh upload cannot publish verified metadata.
- Metadata CAS failure reports failure; matching staged declarations retain the attestation.
- Repointed and plain staged declarations cannot inherit verified root authority.
- Existing remote catalog admission accepts matching staged facts through its current contract.

## Global constraints

Python 3.14; existing dependencies only; targets x86_64 and ppc64le.
No CLI flags, schema migration, ADR index, remote images.publish changes, authority installation,
POWER qualification, public authority repairs, or change to plain staged disk/GRUB eligibility.
No live guest, host infrastructure, image build or expensive staging operation is authorized.

## Failure model

1. Actors and deployments: trusted operator runs CLI with configured DB/store and one remote
   libvirt instance; inventory reconciler and admission read the resulting catalog.
2. Invariants and assets: inspected/uploaded byte identity; no accidental existing-volume
   attestation; public config-row scope; metadata atomicity; private temp cleanup.
3. Accepted failure classes: disk cost is one qcow2 copy; abrupt process death may leave temp
   or remote residue for operator cleanup; post-upload DB failure leaves unverified remote
   bytes; operator remote overwrite after staging is outside stage-time attestation.
4. Covered elsewhere: authority-store ongoing identity checks ADR-0623; remote publication
   ADR-0759; installation #3183; POWER #2818; public repairs #3144/#3148; plain staged #2106.

## Threat model

- Added boundary: local qcow2 to trusted inspection/catalog facts. Private snapshot, bounded
  copying/hash/header parsing, self-contained validation and current root inspector govern it.
- Widened boundary: CLI catalog UPDATE. Parameterized SQL and public/config/identity/volume,
  state/publication fencing prevent writing into a repointed or foreign row.
- Remote transport stays existing mutual TLS; fresh-target refusal prevents attesting a skip.
- Local privileged or same-user malicious replacement, compromised operator/DB/remote daemon,
  and later remote overwrite are outside this trusted-operator contract. No new tenant access.

## Validation

Focused red/green tests cover snapshot bytes/source mutation, bounded chunking, header refusal,
inspection/upload/persist failure ordering, fresh lookup/create races, real Postgres fenced
persistence/reconcile/admission, and advisory config behavior. Run whole-tree just type and
just lint, then one managed pre-push just ci and CI. No live remote proof is claimed.
