# 0724 — The external-boot plan carries an optional debuginfo member

## Status

Proposed

Extends [ADR-0583](0583-external-run-boot-uses-prepared-recovery-points.md) (the plan and
build evidence) and [ADR-0602](0602-local-external-boot-storage-is-reclaimed-by-owned-identities.md)
(the capacity bound). Implements the input side of
[ADR-0723](0723-drgn-live-reads-a-staged-dwarf-vmlinux.md) item 5.

## Context

ADR-0723 requires every install path to stage the Run's uploaded DWARF `vmlinux` at
`/usr/lib/debug/lib/modules/<release>/vmlinux`. An external-boot authority fetches only exact,
version-pinned objects named in the closed `external-boot-plan-v1` value. Neither that plan nor
the `external-boot-evidence-v1` document describes the `vmlinux`.

The plan's identity is a SHA-256 of its canonical bytes. Job payloads store the plan under
`external_boot_plan_v1`, and SQL functions in migration 0135 recompute that identity with the
literal prefix `kdive-external-boot-plan-v1`. The canonical bytes today emit absent optional
members as `null` (`initrd`, `debug_cmdline`), so a new member emitted as `null` changes the
identity of every stored plan.

## Decision

1. `external-boot-evidence-v1` gains a `debuginfo` object, `{sha256, size_bytes}` of the exact
   uploaded `vmlinux` version. The key is present only when a `vmlinux` was uploaded. Build
   completion rejects a `vmlinux` larger than 1.5 GiB (1,610,612,736 bytes).
2. `external-boot-plan-v1` gains an optional `debuginfo` member: the `vmlinux` key, pinned
   version, SHA-256 and size. The canonical bytes omit the member when it is absent. The schema
   name, the identity prefix and the payload key do not change.
3. A build whose evidence has no `debuginfo` key produces a plan with no member. This covers
   builds without a `vmlinux` and builds completed before this change.
4. The materialization reservation that both authorities check counts the member's size. One
   provider-neutral function computes it.
5. Both authorities stage the member and restore the prior file on recovery. Local-libvirt does
   it in #3130 with the modules' staged-then-rename pattern; #3130 decides the recovery-object
   change. Remote-libvirt does it in #3131, which chooses the write and
   recovery mechanism. ADR-0585 allows the remote module appliance one mutable destination,
   `lib/modules/<release>/`, so #3131 amends ADR-0585 or stages inside that tree. Until each
   lands, that authority ignores the member: the ADR-0723 probe reports `debuginfo_unloadable`
   on local and SSH-forward attaches, and a remote guest-agent attach stays silent.

### Amendment (2026-10-07): local-libvirt keeps the prior vmlinux in the guest (#3130)

Item 5 left the local recovery-object change to #3130. This amendment records that choice; it
does not change items 1 to 4.

Local-libvirt moves the prior file at `/usr/lib/debug/lib/modules/<release>/vmlinux` aside in
the same guest directory, under a name that carries the activation id, and recovery renames it
back. It adds no host copy and no `RecoveryObjectBinding` kind. `ProviderStateIdentity` gains an
optional `debuginfo` component, and the materialization gains an optional `debuginfo` reference
and verified digest. Each is left out of the canonical bytes when absent, so stored identities do
not change.

Consequences of this choice: the guest holds both files while the activation is live (free
space is #3125), and directories created for the file stay after recovery. A server or worker
from before #3130 rejects a materialization or recovery point that carries the new members.
This reverses the upgrade order under Consequences for this change only: with #3129 already on
every host, upgrade the server and workers before, or together with, the local authority host.

Considered & rejected:

- **Do nothing: the local authority keeps ignoring the member.** judgment: ADR-0723 item 5 and
  item 5 above require every install path to stage the file; drgn-live keeps failing.

- **Capture the prior file into the host recovery directory, as the modules are.** verified: the
  largest local reservation is 34,055,536,640 bytes against 34,359,738,368 bytes of shipped
  capacity (Consequences below), so a second 1.5 GiB capture does not fit.
- **Publish the whole `/usr/lib/debug/lib/modules/<release>` directory with the module
  machinery.** judgment: it replaces files the plan does not name, and its capture bound is the
  8 GiB module archive.
- **Overwrite the file and leave the prior content unrecoverable.** judgment: item 5 requires
  restoring the prior file.

## Consequences

- Every plan stored before this change parses and keeps its identity. No migration is needed.
- A process from before this change rejects a plan that carries the member (`extra="forbid"`).
  Upgrade authority hosts before workers and the server; an old authority host answers
  `invalid-request`. Rolling a worker back past this change while such boot jobs are in flight
  is not supported.
- A Run completed before this change gets no member even when it uploaded a `vmlinux`. To get
  one, complete the build again in a new Run.
- Build completion reads the whole `vmlinux` (up to 1.5 GiB) once to hash it, for every
  external build. Finalizations are serialized per server process, so this extends the
  synchronous completion cost that ADR-0656 measured for the bundle alone. ADR-0656 records
  6,914 ms of store time for a 1,845,478,477-byte bundle. At that rate 1.5 GiB adds about 6 s of
  reads plus hashing to the 300 s budget. This is an estimate, not a measurement.
- The 1.5 GiB bound keeps the largest local reservation (34,055,536,640 bytes) under the shipped
  32 GiB `KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES`. The bound also applies to builds that
  only use the legacy install path.

## Considered & rejected

- **`external-boot-plan-v2`.** verified: migration
  `src/kdive/db/schema/0135_external_boot_authority_preparation.sql` hashes
  `kdive-external-boot-plan-v1` and reads `payload->'external_boot_plan_v1'` (kdive `708a2084d`),
  so a second schema needs new SQL functions and two plan readers for one optional field.
- **Emit an absent member as `null`, like `initrd`.** verified: the golden vector in
  `tests/providers/ports/test_external_boot.py` fixes the identity of a plan without the key.
  An added `null` changes that identity, so the marker check in `src/kdive/jobs/payloads.py`
  (`plan.identity != marker.plan_identity`) rejects every in-flight boot job.
- **Leave the size unbounded, or reuse the 8 GiB module bound.** verified: the local
  reservation without debuginfo is 32,444,923,904 bytes (constants in
  `src/kdive/providers/shared/external_boot_bounds.py` and `build_artifacts/validation.py`,
  kdive `708a2084d`), so more than 1,914,814,464 bytes exceeds the shipped 32 GiB capacity.
  ADR-0723 puts a DWARF `vmlinux` at hundreds of MB.
- **Remote authority marks the member as not staged.** judgment: ADR-0723 item 5 requires every
  install path to stage the `vmlinux`; a permanent refusal would need its own superseding record.
