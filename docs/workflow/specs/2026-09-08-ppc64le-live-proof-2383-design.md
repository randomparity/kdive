# ppc64le live proof: banner scan, fadump and kdump capture (#2383)

## Problem

#2382 rewrote the ppc64le ELF banner scan; #2312's evidence predates it and never crossed a
chunk boundary. #1204 still owes a fadump crash→capture proof.

## Scope

One unit. Host of record (operator decision, charter freeze 3): an emulated ppc64le guest
(QEMU `pseries`, `accel=tcg`, Fedora 44) with a detached checkout of merged `main`; the record
commits on this branch. Nested TCG makes boots expensive, so the cheapest driver runs first as
a timing check. Raising `_PPC64LE_BOOT_DEADLINE_S` and `DRAIN_DEADLINE_S`
(ceiling 7200 s) is pre-authorized and the only edit permitted there; a phase still over
budget is a recorded timeout, not a capture failure.

Precondition: `uv sync --locked --group live` completes there first; grpcio's BoringSSL rejects ppc64le, needing system OpenSSL and zlib per `cross-platform.md`.

Build `KDIVE_GUEST_IMAGE_PPC64LE` via `python -m kdive build-fs`, so #2381's
`fadump-capture.service` comes from the customize steps (ADR-0345); record its commit. Build `KDIVE_PPC64LE_BUNDLE` from a Fedora 44 ppc64le kernel, recording the `tar` line
excluding `lib/modules/*/vmlinuz` and the escaping `build`/`source` symlinks. Start the
backends *and* the three services at that revision, then run the drivers by node id, not the
tier.

Write a dated record under `docs/design/`, redacted to the charter's token scheme, linked from
`platform-support.md` and `live-testing.md`'s fadump-outcome sentence, rewritten for the TCG
host of record.

#2381 captured under TCG on a hand-run Fedora 43 guest; this repeats it through the stack at 4 GiB, nested, on Fedora 44, so the outcome is open; a negative one still lands the
record. **Deferred to a follow-up issue filed with this PR:** ADR-0349's outcome and the fadump
docstring both contradict a pass.

## Success

1. `runs.complete_build` accepts the bundle; the validated release is its whole
   `lib/modules/<release>` name.
2. `test_ppc64le_fadump_captures_a_vmcore_under_tcg` passes rather than skips.
3. `test_ppc64le_kdump_captures_a_vmcore_under_tcg` passes.
4. A dated, redacted record lands under `docs/design/`, both links made.
5. The services' revision matches the run checkout, itself merged `main`.
6. The record supplies #1204's evidence; #1204 stays open, closed separately.

## Validation

Green for 1–3 is the pytest summary `1 passed` for that node id (`-rs`); `skipped` or
`no tests ran` is Red.

| # | Mode | Case, Red |
|---|---|---|
| 1 | focused-test | `test_ppc64le_uploaded_kernel_bundle_boots_over_the_wire`. Red: `complete_build` raises `boot/vmlinuz release does not match its module tree`; the record quotes it. A stock bundle exercises only the past-16-MiB scan; `test_ppc64le_elf_banner_release_is_whole_across_every_chunk_alignment` covers the cut token. |
| 2 | focused-test | `test_ppc64le_fadump_captures_a_vmcore_under_tcg`. Red: `skipped`, or no `vmcore-fadump` ref. |
| 3 | focused-test | `test_ppc64le_kdump_captures_a_vmcore_under_tcg`. Red: a raw ref surviving `raw_vmcore_refs`. |
| 4 | focused-test | `just check-pr-body` over the record and each published body, and `docs-links` for the linking files. Red: a credential, DSN, env-dump shape, or bad link. |
| 4b | task-test-not-applicable | Host identifiers in prose: no tool here models host identity, so no observation could fail meaningfully; read against the charter's token list first. |
| 5 | focused-test | The run checkout is `228f08ad7`, clean; the services report it. Red: a mismatch. |
