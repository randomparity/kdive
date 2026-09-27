# Pinned external Linux fixtures for #2806

Status: Operator-approved design (2026-09-27); independent design review completed.
Scope: [#2806](https://github.com/randomparity/kdive/issues/2806),
[frozen charter](https://github.com/randomparity/kdive/issues/2806#issuecomment-5856921316).
The operator approved the charter's exclusions in the interactive session on 2026-09-27.

## Problem and outcome

The fetch helper accepts an existing checkout without checking its revision. The upload spine
accepts a built directory without retained evidence linking source, configuration and outputs.
Produce two reusable x86_64 fixtures and demonstrate successful real uploads of each. Compilation
remains outside KDIVE; this work does not claim that either uploaded kernel booted.

## Scope and ownership

C1–C5 below refer to the frozen charter's criteria. Extend the source helper for C1/C2 and add a
small external fixture command for C2–C4. The existing spine retains packaging and upload ownership
(C3/C5); the new live proof calls that seam. No production API, schema or provider changes.

Exclusions and owners remain: native POWER execution (#2818); broader guest/provider boot-debug
matrix (#2809/#2810 and later epic entries); coverage/release-gate infrastructure (#2804/#2819);
new providers, a build service and shared-host reprovisioning (outside #2803, separate operator
authority). The external fixture command preserves explicit x86_64/ppc64le target selection;
ppc64le execution remains unproven here.

## Proposed design

1. **Pinned inputs (C1).** Add a small TOML fixture selection under
   `fixtures/kernel/` containing release label, upstream repository, exact commit, and selection
   date/source. Verified on 2026-09-27 against kernel.org and upstream peeled tags:
   long-term `v6.18.54` = `1b357ecb321392158d507b04672ffee57bfa071d`;
   stable `v7.2.8` = `9a66fdc0d7fd55f54235524a73435af99051e46f`.
   Updates are explicit reviewed input changes, not automatic latest-version selection.
2. **Source acquisition (C1/C2).** Keep `scripts/fetch-kernel-tree.sh` and its stdout-path contract.
   Resolve the requested ref to a commit; fetch exact commits as well as tags. On reuse compare
   HEAD with that commit and reject tracked modifications or untracked source additions. Ignore
   Git-ignored build products for existing in-tree callers. Reject mismatches without checking out,
   resetting or deleting existing work. Accept ordinary repositories and Git worktrees.
3. **External builds (C2/C4).** Add `scripts/kernel_fixtures.py` with build and verify commands.
   Build requires baseline, explicit target architecture, source path, fresh output directory,
   config input and positive job count. It uses Linux Kbuild's separate `O=` directory,
   copies the input config there, runs `olddefconfig`, then builds the boot image, vmlinux
   and modules. x86_64 proof uses a checked-in config fragment layered on x86_64_defconfig:
   built-in virtio PCI/block/network, ext4 and XFS, DWARF5, BTF, module support and a real test
   module. Check the effective settings after olddefconfig instead of assuming the fragment won.
   Do not strip retained vmlinux or module objects; the existing tar helper may strip staged copies.
4. **Provenance and reuse (C2–C4).** Write a JSON manifest only after successful build validation.
   Record source commit, architecture, input/effective config hashes, kernel release, compiler and
   binutils/pahole versions, builder revision, source commit timestamp, GNU vmlinux build ID,
   and relative-path SHA-256 digests for boot/vmlinux/config/module outputs. Use fixed anonymous
   KBUILD_BUILD_USER/HOST values and a source-derived timestamp. Record package versions privately;
   retain a sanitized toolchain/package identity in shareable evidence. A canonical manifest hash
   identifies the fixture. Verification recomputes the recorded output digests and compares selected
   inputs; missing, changed or incomplete artifacts fail. Never infer validity from a directory name.
5. **Upload proof (C3/C5).** Extend `build_and_upload_kernel` with an optional explicit built-tree
   input and evidence destination while preserving existing callers. The fixture carrier verifies
   its manifest, then uploads the existing combined kernel bundle, matching vmlinux and effective
   config and supplies the measured build ID. Retain the exact submitted bundle and its digest.
   A live-stack test parameterized over the two selected baselines creates unbound Runs, calls the
   existing real HTTP/presigned-upload path, and checks persisted succeeded build state plus the
   registered artifact identities/content hashes. This uses no Allocation, System or VM.
6. **Candidate identity and cleanup (C5).** Before and after the proof compare the running server's
   health-reported commit with the full candidate commit after validated, unambiguous Git resolution
   of abbreviations (the existing skew resolver); reject unknown, malformed or mismatched values.
   Include identities for other roles only if they participate in the exercised path; mark unused
   roles, guest and accelerator not applicable with the unbound-upload reason. Run against isolated
   test backing services, retain intended fixture/evidence files, and verify removal of test-owned
   services and scratch uploads at teardown. Never clear shared database/store state.
7. **Cold-cache proof (C4).** On the selected x86 host build both baselines from fresh source/output
   directories without compiler-cache reuse. Repeat from separate fresh directories and compare
   source/config/toolchain provenance and output identities, retaining both results and durations.
   Different bytes are a new fixture identity, not an automatic reproducibility failure, if the
   retained input/package evidence explains the difference. Missing provenance or a failed build
   fails that scenario. Do not claim bit-identical reproducibility from mutable repositories.
8. **Resource bounds (C4/C5).** One build at a time; choose jobs after CPU/RAM/disk preflight on the
   selected host. Retain that choice and measured costs. The local ARM workstation is not native
   x86 proof. Check build prerequisites before downloading/building; add any newly required package
   to its owning Ansible role in this change. Do not reprovision supplied shared hosts.

## Failure model

- **Actors and deployments:** trusted operators building upstream Linux on supplied native Linux
  development hosts; existing authenticated live-stack clients against isolated real backing
  services. One owner per source/output directory; no shared-directory concurrent builds.
- **Invariants and assets:** source identity, matching debug/module/boot/config artifacts, honest
  candidate-bound evidence, private host data, existing source trees and unrelated host resources.
- **Accepted failure classes:** bit differences caused by recorded mutable toolchain/package inputs
  create distinct fixtures and require requalification; build/network/storage failures remain
  failed or blocked scenarios, never evidence of success. Crash leftovers may require explicit
  cleanup of this run's named private directories; an incomplete manifest cannot be reused.
- **Covered elsewhere:** boot/module loading and debug behavior (#2809/#2810/#2813/#2814), POWER
  qualification (#2818), coverage accounting and release enforcement (#2804/#2819). Existing
  product defects remain linked blockers rather than being absorbed into this change.

## Verification and success

Use real tiny local Git repositories to prove matching reuse, wrong commit, dirty source, Git
worktrees, tag and exact-SHA acquisition. Exercise builder command boundaries with controlled
subprocess results to prove architecture/config/job validation, isolation, failed-build behavior,
manifest digest verification and source/output tampering. Retain existing spine tests and add
coverage for explicit fixture input and upload evidence; controlled missing or mismatched candidate
identity must reject the live carrier before mutation.

Success requires focused tests, lint/type/shell checks, both real x86_64 builds and cold repeats,
both real uploads with persisted artifact proof, and verified owned cleanup. Required missing
prerequisites fail or block. No full VM suite is required for this upload-only slice. The installed
pre-push hook owns full local `just ci`; CI and live results are reported separately.

## Alternatives

- Recommended: external build command plus existing spine. Fixtures can be reused by later tests
  while build and upload failures remain independently observable.
- Build inside each live test: rejected as a complexity/cost judgment; it couples an expensive
  fixture build to each upload execution and makes later reuse harder.
- New build service or generalized fixture scheduler: excluded by the approved charter.
