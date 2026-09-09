# ppc64le live proof: banner scan, fadump and kdump capture (#2383)

## Problem

#2382 rewrote the ppc64le ELF banner scan so a boundary-cut release token fails instead of
registering a prefix. #2312's evidence predates that fix, never crossing a boundary,
so it misses `main`. #1204 still owes a fadump crash→capture proof, whose driver skips off
ppc64le.

## Scope

One unit. Host of record (operator decision, charter freeze 2): an
emulated ppc64le guest (QEMU `pseries`, `accel=tcg`, Fedora 44) with its own detached checkout
of merged `main`; the record commits on this branch. Nested TCG makes each boot
expensive, so the cheapest driver runs first as a timing check. Raising
`_PPC64LE_BOOT_DEADLINE_S` (ceiling 7200 s) is pre-authorized and the only edit permitted
there; a phase still over it is recorded as a timeout, not a capture failure.

Build `KDIVE_GUEST_IMAGE_PPC64LE` there via `python -m kdive build-fs`, so #2381's
`fadump-capture.service` comes from the family's customize steps (ADR-0345);
record its build commit. Build `KDIVE_PPC64LE_BUNDLE` from a Fedora 44 ppc64le kernel,
recording the `tar` line excluding `lib/modules/*/vmlinuz` and the escaping `build`/`source`
symlinks the contract rejects. Start the backends *and* the three services
at that revision, then run the three drivers by node id, not the whole tier, whose fourth
proof no criterion needs.

Write a dated record under `docs/design/`, linked from `platform-support.md` and
`live-testing.md`, redacted to the charter's token scheme.

#2381's emulated-POWER10 capture supersedes the 2026-07-14 TCG Oops verdict, so a pass is
expected, on its first Fedora 44 repeat. A negative outcome still lands the record.

**Deferred to one follow-up issue filed with this PR:** ADR-0349's accepted "native-POWER
required" outcome and the fadump docstring contradict a pass; no ppc64le wheel index
serves this lock.

Exclusions are the charter's freeze-2 set, unchanged.

## Success

1. The bundle validates and the Run's persisted release equals its `lib/modules/<release>`
   name in full.
2. `test_ppc64le_fadump_captures_a_vmcore_under_tcg` passes rather than skips.
3. `test_ppc64le_kdump_captures_a_vmcore_under_tcg` passes.
4. A dated, redacted record lands under `docs/design/`, with both doc links made.
5. The services' revision matches the run checkout, which is merged `main`.
6. The record supplies #1204's evidence; #1204 stays open, closed separately.

## Validation

Green for 1–3 is the pytest summary `1 passed` for that node id (`-rs`); `skipped` or
`no tests ran` is Red — a skip exits 0 too.

| # | Mode | Case, Red |
|---|---|---|
| 1 | focused-test | `test_ppc64le_uploaded_kernel_bundle_boots_over_the_wire`. Red: persisted release shorter than the bundle's `lib/modules/<release>`. Record the banner offset; a stock bundle covers only the past-16-MiB case — `test_ppc64le_elf_banner_release_is_whole_across_every_chunk_alignment` covers the cut token. |
| 2 | focused-test | `test_ppc64le_fadump_captures_a_vmcore_under_tcg`. Red: `skipped`, or no `vmcore-fadump` ref. |
| 3 | focused-test | `test_ppc64le_kdump_captures_a_vmcore_under_tcg`. Red: a raw ref surviving `raw_vmcore_refs`. |
| 4, 6 | focused-test | `just check-pr-body` over the record and each published body, plus `docs-links`/`docs-paths`. Red: an unredacted presigned URL, DSN, host identifier, broken link, or a record not naming #1204 and each outcome. |
| 5 | focused-test | The run checkout is `228f08ad7`, clean; the services report it. Red: a mismatch. |
