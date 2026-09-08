# ppc64le live proof: banner scan, fadump and kdump capture (#2383)

## Problem

#2382 rewrote the ppc64le ELF banner scan so a release token cut by a read-chunk boundary
fails instead of registering a silent prefix. #2312's evidence ran against the pre-fix scan,
never crossing a boundary, so it misses `main`. #1204 still owes a fadump
crash→capture proof, and that driver skips unless `platform.machine() == "ppc64le"`.

## Scope

One unit. Host of record (operator decision, `WORK:SCOPE q2383-4ac00b9e`): an emulated
ppc64le guest (QEMU `pseries`, `accel=tcg`, Fedora 44). The inner guest is therefore
TCG-under-TCG; size the outer for a 4 GiB inner guest plus the compose backends, and run
the bundle-boot driver first as a timing check against the fixed 1800 s
`_PPC64LE_BOOT_DEADLINE_S`. A timeout is recorded as such, not a capture failure.

Build `KDIVE_GUEST_IMAGE_PPC64LE` with `python -m kdive build-fs` from this checkout, so
#2381's `fadump-capture.service` is injected by the family's customize steps (ADR-0345), not
hand-applied; record its build commit. Build `KDIVE_PPC64LE_BUNDLE` from a Fedora 44 ppc64le
kernel, excluding `lib/modules/*/vmlinuz` and the escaping `build`/`source` symlinks. Start
the backends *and* server, worker and reconciler at the checkout revision, run
`just test-live-tcg`, and write a dated record under `docs/design/`, linked from the
`platform-support.md` capture rows and `live-testing.md`'s fadump-outcome sentence.

In it, hostnames, addresses, usernames, home and `/var/lib/kdive` paths, SSH
endpoints, DSNs and presigned URLs become stable `<REDACTED-*>`/`sys-N` tokens reused per
original value; versions, build levels, model numbers and errors stay verbatim.

A pass is expected because #2381's emulated-POWER10 capture supersedes the 2026-07-14 TCG Oops
verdict; this is its first repeat on Fedora 44. A negative outcome still lands the record,
leaving #1204 open.

Excluded (operator-approved): no `src/kdive/` change; #1181's record not reopened; the fadump
gate unchanged; no CI, runner-role or builder-harness change.

## Success

1. The bundle validates and the Run's persisted release equals its
   `lib/modules/<release>` name in full, not a prefix.
2. `test_ppc64le_fadump_captures_a_vmcore_under_tcg` passes rather than skips.
3. `test_ppc64le_kdump_captures_a_vmcore_under_tcg` passes.
4. A dated, redacted record lands under `docs/design/` with the two doc links made.
5. The running services' revision equals the checkout HEAD, itself merged `main`.

## Validation

Green for 1–3 is the pytest summary `1 passed` for the named node id (`-rs`); `skipped` or
`no tests ran` is Red — a skip exits 0 too.

| # | Mode | Case, Red, Green |
|---|---|---|
| 1 | focused-test | `test_ppc64le_uploaded_kernel_bundle_boots_over_the_wire`. Red: persisted release shorter than the bundle's `lib/modules/<release>`. Green: `-k bundle`. Record the banner offset; a stock bundle covers only the past-16-MiB case — `test_ppc64le_elf_banner_release_is_whole_across_every_chunk_alignment` covers the cut token. |
| 2 | focused-test | `test_ppc64le_fadump_captures_a_vmcore_under_tcg`. Red: `skipped`, or no `vmcore-fadump` ref. Green: `-k fadump`. |
| 3 | focused-test | `test_ppc64le_kdump_captures_a_vmcore_under_tcg`. Red: a raw ref surviving `raw_vmcore_refs`. Green: `-k kdump`. |
| 4 | focused-test | `just check-pr-body` over the record and every published body, plus `docs-links`/`docs-paths`. Red: an unredacted presigned URL, DSN, host identifier, or broken link. |
| 5 | focused-test | `git rev-parse HEAD` equals `228f08ad7` and the revision the running services report. Red: any mismatch. |
