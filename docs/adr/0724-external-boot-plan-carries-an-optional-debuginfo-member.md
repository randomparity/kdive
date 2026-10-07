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
   completion rejects a `vmlinux` larger than 8 GiB.
2. `external-boot-plan-v1` gains an optional `debuginfo` member: the `vmlinux` key, pinned
   version, SHA-256 and size. The canonical bytes omit the member when it is absent. The schema
   name, the identity prefix and the payload key do not change.
3. A build whose evidence has no `debuginfo` key produces a plan with no member. This covers
   builds without a `vmlinux` and builds completed before this change.
4. The materialization reservation that both authorities check counts the member's size. One
   provider-neutral function computes it.
5. Both authorities stage the member with the same staged-then-rename write and recovery rules
   as modules: local-libvirt in #3130, remote-libvirt in #3131. Until each lands, that authority
   ignores the member, and the ADR-0723 runtime probe reports `debuginfo_unloadable`.

## Consequences

- Every plan stored before this change parses and keeps its identity. No migration is needed.
- A process from before this change rejects a plan that carries the member (`extra="forbid"`),
  so a mixed-version deployment fails that boot closed instead of booting without debuginfo.
- A Run completed before this change gets no member even when it uploaded a `vmlinux`. To get
  one, complete the build again in a new Run.
- Build completion reads the whole `vmlinux` once to hash it, as it does for the initrd.
- A large `vmlinux` can push an activation over `KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES`.
  The authority then refuses before it writes anything.

## Considered & rejected

- **`external-boot-plan-v2`.** verified: migration
  `src/kdive/db/schema/0135_external_boot_authority_preparation.sql` hashes
  `kdive-external-boot-plan-v1` and reads `payload->'external_boot_plan_v1'` (kdive `708a2084d`),
  so a second schema needs new SQL functions and two plan readers for one optional field.
- **Emit an absent member as `null`, like `initrd`.** verified: the golden vector in
  `tests/providers/ports/test_external_boot.py` fixes the identity of a plan without the key;
  any added key changes those bytes, which breaks the SQL identity check for in-flight jobs.
- **Leave the size unbounded.** judgment: every other plan member is bounded, and a size only
  caught at install time fails late with a vague error.
- **Remote authority marks the member as not staged.** judgment: ADR-0723 item 5 requires every
  install path to stage the `vmlinux`; a permanent refusal would need its own superseding record.
