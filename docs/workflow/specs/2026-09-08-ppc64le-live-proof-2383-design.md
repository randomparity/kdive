# ppc64le live proof: banner scan, fadump and kdump capture (#2383)

## Problem

#2382 rewrote the ppc64le ELF banner scan; #2312's evidence predates it and never crossed a
chunk boundary. #1204 still owes a fadump crash→capture proof.

## Scope

Host of record (charter freeze 3): an emulated ppc64le guest (QEMU `pseries`,
`accel=tcg`, Fedora 44) running merged `main`.

Freeze 4 amended the no-production-code exclusion after the run proved criteria 1–3
unreachable: `virt-customize --ssh-inject` writes each System's bootstrap key and measured
1474 s against a fixed 300 s budget. Permitted there only: scale it off the *worker host's*
KVM, not the System's `accel` (the appliance is host-arch), reusing ADR-0341's multiplier, plus
ADR-0636. `SLOW_BUILD_TOOL_TIMEOUT_S` shares the defect but gates no criterion. Raising `_PPC64LE_BOOT_DEADLINE_S` and `DRAIN_DEADLINE_S` to 7200 s was
pre-authorized; a phase still over budget is a recorded timeout.

Build `KDIVE_GUEST_IMAGE_PPC64LE` via `python -m kdive build-fs` so #2381's
`fadump-capture.service` comes from the customize steps (ADR-0345), and `KDIVE_PPC64LE_BUNDLE`
from a Fedora 44 ppc64le kernel, excluding `lib/modules/*/vmlinuz` and the escaping
`build`/`source` symlinks. Start the backends *and* the three services at the run
revision; run the drivers by node id, not the tier.

Write a dated record under `docs/design/`, redacted to the charter's token scheme and stating
every host deviation, linked from `platform-support.md` and `live-testing.md`'s fadump-outcome
sentence, rewritten for the TCG host.

#2381's capture was hand-run on Fedora 43; this repeats it through the stack at 4 GiB, nested,
on Fedora 44, so the outcome is open — a negative one still lands the record. **Deferred to a
follow-up:** ADR-0349's outcome and the fadump docstring contradict a pass.

## Success

1. `runs.complete_build` accepts the bundle; the validated release is its whole
   `lib/modules/<release>` name.
2. `test_ppc64le_fadump_captures_a_vmcore_under_tcg` passes rather than skips.
3. `test_ppc64le_kdump_captures_a_vmcore_under_tcg` passes.
4. A dated, redacted record lands under `docs/design/`, both links made.
5. The services' revision matches the run checkout (`main` + the freeze-4 commits).
6. The record supplies #1204's evidence; #1204 stays open, closed separately.

## Validation

Green for 1–3 is `1 passed` for that node id (`-rs`); `skipped` or `no tests ran` is Red.

| # | Mode | Case, Red |
|---|---|---|
| 1 | focused-test | `test_ppc64le_uploaded_kernel_bundle_boots_over_the_wire`. Red: `complete_build` raises `boot/vmlinuz release does not match its module tree`. The cut token stays covered by `test_ppc64le_elf_banner_release_is_whole_across_every_chunk_alignment`. |
| 2 | focused-test | `test_ppc64le_fadump_captures_a_vmcore_under_tcg`. Red: `skipped`, or no `vmcore-fadump` ref. |
| 3 | focused-test | `test_ppc64le_kdump_captures_a_vmcore_under_tcg`. Red: a raw ref surviving `raw_vmcore_refs`. |
| 4 | focused-test | `tests/providers/local_libvirt/test_deadlines.py`. Red: a KVM host scaled, an emulated host unscaled, or 300 s × the multiplier under 1474 s. |
| 5 | focused-test | `just check-pr-body` over the record and each published body; `docs-links` for the linking files. Red: a credential, DSN, env-dump shape, or bad link. |
| 5b | task-test-not-applicable | Host identifiers in prose: no tool models host identity, so no observation could fail; read against the charter's tokens. |
| 6 | focused-test | The services' build stamp equals the run checkout's short HEAD. Red: a mismatch. |
