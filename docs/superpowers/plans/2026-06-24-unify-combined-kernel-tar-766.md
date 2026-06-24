# Plan — Unify the build-artifact format on the combined kernel+modules tar (#766)

**Spec / decision of record:** [ADR-0234 §2](../../adr/0234-external-build-default-and-contributor-role.md)
("One artifact format across providers" — explicitly "Implemented in #766"). This plan adds no
new architectural decision; it implements the settled one. Epic [#771](https://github.com/randomparity/kdive/issues/771).

## Goal

Both providers (and the external-upload lane) consume **one** combined `kernel` artifact: a
gzip-compressed tar containing `boot/vmlinuz` (the renamed `arch/x86/boot/bzImage`) and
`lib/modules/<ver>/…`, excluding the `build`/`source` back-reference symlinks. This is the shape
remote-libvirt already produces. The separate local-libvirt `modules` artifact and the
`modules_ref` field are **removed** (replace, don't deprecate).

## Current state (verified on this branch)

- `remote_libvirt/build.py` already produces the combined tar as `kernel` and returns
  `BuildOutput(kernel_ref, debuginfo_ref, build_id)` — **no** `modules_ref`. The combined-tar
  helpers (`_real_build_bundle`, `transport_make_bundle`, `_local_make_bundle`) live in that module.
- `local_libvirt/build.py` produces a raw bzImage `kernel` and, only when `CONFIG_CRASH_DUMP=y`,
  a separate `modules` artifact; it sets `BuildOutput.modules_ref`.
- `local_libvirt/lifecycle/install.py` fetches the raw bzImage to `staging/kernel` (for the libvirt
  `<kernel>` element) and, on KDUMP/debuginfo, fetches the separate modules tar and feeds it to
  `_RealGuestKernelWriter.inject(overlay, kernel_image, modules_tar, vmlinux)`.
- `build_artifacts/validation.py:_check_magic` validates an external `kernel` upload as a **raw
  bzImage** (magic `HdrS` at offset `0x202`). Under the unified format the `kernel` upload is a
  gzip tar, so this check must change.
- `modules_ref` threads through `BuildOutput`, `InstallRequest`, `BuildStepResult`
  (a JSON step-result field — **no DB column, no migration**), `installed_modules_ref()`,
  `runs_build.py`, `runs_install.py`.
- `RUN_ARTIFACT_NAMES` already excludes `"modules"` (the issue text is stale here);
  `expected_uploads.py` still describes `kernel` as "a bzImage".

## Design decisions taken for this implementation (within ADR-0234 §2)

1. **Local install does a host-side split, not a stray extract.** Install fetches the combined tar,
   then host-side (pure Python, no libguestfs): extracts `boot/vmlinuz` → `staging/kernel` (always,
   for the `<kernel>` element) and, when modules are needed (KDUMP or debuginfo), repacks the
   `lib/modules/…` members into a modules-only `staging/modules.tar.gz` fed to the **unchanged**
   injector. This keeps the in-guest result byte-identical to today (no stray unversioned
   `/boot/vmlinuz`) and keeps the split logic unit-testable without a live host.
2. **Local build always runs `modules_install`.** The combined tar always carries modules (parity
   with remote), so the prior `CONFIG_CRASH_DUMP=y`-only conditional is dropped. A non-kdump local
   boot still does not *inject* modules into the overlay (the install gate stays KDUMP-or-debuginfo);
   it only stops carving the kernel format on kdump-capability.
3. **External `kernel` validation = gzip tar with the right shape.** Replace the bzImage magic with:
   gzip magic (`\x1f\x8b`) at offset 0, then a bounded streaming pass (ranged reads feeding
   `tarfile` in stream mode, early-exit, capped total read) that confirms `boot/vmlinuz` is present
   and is itself a bzImage (`HdrS` at member offset `0x202`) and that at least one
   `lib/modules/<ver>/…` member exists. This preserves the old "kernel is a real bzImage" safety
   and fails fast at `runs.complete_build` instead of deferring to install.
4. **Reuse, don't copy, the bundler.** Move the combined-tar helpers into a shared
   `providers/shared/build_host/publishing/kernel_bundle.py` consumed by both providers (the
   build-host seam already shared per ADR-0081/0101). This is not the `libvirt_common` layer
   ADR-0076 rejected — it is the build-host packaging seam, used by both build planes.

## Tasks (each commit green: `just lint` + `just type` + `just test`)

### T1 — Extract the shared kernel-bundle seam (pure refactor)
- New `providers/shared/build_host/publishing/kernel_bundle.py` with: `make_kernel_bundle_bytes`
  (was `_real_build_bundle`), `transport_kernel_bundle` (was `transport_make_bundle`),
  `local_kernel_bundle` (was `_local_make_bundle`), the `_MODULE_BACKREF_LINKS` set, the
  `_add_bundle_member`/`_build_bundle_member_dirs` helpers, the bundle filename + tar timeout.
- `remote_libvirt/build.py` imports them; its public behavior is unchanged.
- New `tests/providers/shared/build_host/test_kernel_bundle.py` covering: bzImage→`boot/vmlinuz`
  rename, `lib/modules/<ver>/…` membership, back-ref symlink exclusion, OSError→`BUILD_FAILURE`.
- **Acceptance:** remote build tests pass unchanged; the shared module has its own tests; no
  behavior change. Files: shared kernel_bundle.py (+test), remote_libvirt/build.py.

> **Commit-boundary note (challenge iter-1, finding 1):** removing `InstallRequest.modules_ref`
> while `install.py` still reads it breaks whole-tree `ty`. The contract removal and the install
> rewrite are **one logical change** and land together in T3. T2 keeps the field in place (local
> build simply stops *setting* it) so every commit type-checks and tests green.

### T2 — Converge local-libvirt build onto the combined tar (contract field stays)
- `local_libvirt/build.py`: always `modules_install` + `make_kernel_bundle_bytes`; publish `kernel`
  = combined tar; remove `_maybe_publish_modules`, `_local_modules_bundle`, `transport_modules_bundle`,
  the `modules` publish, the kdump-capability conditional, the staging seams that only fed modules.
  Reuse the shared bundle seam (worker + transport). `build()` returns `BuildOutput(...)` leaving
  `modules_ref` at its `None` default (the field is still present; it is removed in T3).
- `jobs/handlers/runs_build.py`: stop persisting a `modules_ref` (output no longer carries one).
- Update `tests/providers/local_libvirt/test_build.py`, `tests/jobs/handlers/test_runs_build.py`.
- **Acceptance:** local build publishes exactly two artifacts (`kernel` combined tar + `vmlinux`);
  no `modules` artifact is produced; remote build untouched; the now-unused `modules_ref` field is
  still defined (default `None`) and the tree type-checks/tests green.
- **CONFIG_MODULES=n parity (challenge iter-1, finding adj.):** `modules_install` now runs for every
  local config, including all-builtin (`CONFIG_MODULES=n`). This is exactly what remote already does,
  so it is proven; assert in the build test that an all-builtin staging tree (only `lib/modules/<ver>/`
  with `modules.builtin`, no `.ko`) still bundles without error.

### T3 — Local install consumes the combined tar; remove `modules_ref` from the contract
- `local_libvirt/lifecycle/install.py`: fetch `kernel_ref` (combined tar) to `staging/kernel.tar.gz`;
  host-side extract `boot/vmlinuz` → `staging/kernel`; on KDUMP-or-debuginfo, repack
  `lib/modules/…` → `staging/modules.tar.gz` and `inject(...)` (injector unchanged). Recompute the
  kdump-absent guard against "tar carried no modules and no initrd".
- New pure-Python extract/repack helpers (testable without libguestfs): extract a named member to a
  path; repack `lib/modules/…` members into a gzip tar; surface a missing `boot/vmlinuz` as a
  categorized error.
- **In the same commit** (one logical change): remove `InstallRequest.modules_ref`
  (`providers/ports/lifecycle.py`), `BuildOutput.modules_ref` (`build_artifacts/results.py`),
  `BuildStepResult.modules_ref` + its (de)serialization + the `modules` ref entry +
  `installed_modules_ref()` (`services/runs/steps.py`), and the `modules_ref` plumbing in
  `runs_install.py`.
- **Trace before removing the `modules` ref entry (challenge iter-1, finding 3):** grep every reader
  of the `BuildStepResult` refs mapping, `installed_modules_ref`, and the `"modules"` artifact name in
  response builders (`runs.get`, retrieve plane, artifact listings). Update or confirm no consumer;
  add/adjust a test pinning the run's reported refs.
- Update `tests/providers/local_libvirt/test_install.py`, `tests/services/runs/test_steps.py`,
  `tests/providers/test_ports_contracts.py`, `tests/mcp/lifecycle/test_runs_tools.py`.
- **Acceptance:** install stages `<kernel>` from the tar's `boot/vmlinuz`; kdump injects modules
  extracted from the same tar; non-kdump boot needs no separate modules upload; no `modules_ref`
  anywhere; the tree type-checks/tests green.

### T4 — External `kernel` upload validation = combined tar
- `build_artifacts/validation.py`: `_check_magic("kernel", …)` → gzip magic at offset 0, then a
  **bounded streaming shape check**: stream the gzip tar via sequential `get_range` reads feeding
  `tarfile` in stream mode, confirming `boot/vmlinuz` is present and is a bzImage (`HdrS` at member
  offset `0x202`) and at least one `lib/modules/<ver>/…` member exists. A malformed/incomplete tar is
  a `BUILD_FAILURE` with an actionable message.
- **Bound the decompressed output, not the ranged-read count (challenge iter-1, finding 2):**
  `boot/vmlinuz` is the first member, so the first `lib/modules` header is only reachable after
  decompressing the whole bzImage payload (tens of MB). Set a decompressed-output cap comfortably
  above a real bzImage (`_KERNEL_TAR_SCAN_MAX_BYTES`, low-hundreds-of-MB) and abort with
  `BUILD_FAILURE` if both members are not seen before the cap — this both prevents false rejects of a
  large-but-legal bzImage and caps a gzip-bomb (tiny gzip → gigabytes of tar). Read in fixed chunks;
  count decompressed bytes via the streaming tar offset, not the compressed bytes fetched.
- Tests in the validation test module: valid combined tar passes; raw bzImage now rejected;
  gzip-of-not-a-tar rejected; tar missing `boot/vmlinuz` or `lib/modules` rejected; a `boot/vmlinuz`
  that is not a bzImage rejected; a decompression-bomb (members never appear) aborts at the cap.
- **Acceptance:** an external builder uploading the combined `kernel` tar passes
  `runs.complete_build`; a raw bzImage is rejected with a clear message; the scan is bounded.

### T5 — Docs
- `expected_uploads.py`: `kernel` description → "Combined kernel+modules tar (gzip): `boot/vmlinuz`
  + `lib/modules/<ver>/`." (the richer advisory is #769's scope; keep this minimal and correct).
- This plan doc; reference from nothing new (ADR-0234 already cites #766).
- **Acceptance:** no tool response still calls `kernel` a bare bzImage.

## Rollback / cleanup
- Single branch; revert is a clean `git revert` of the feature commits (no migration to unwind —
  artifact bytes change, not schema). Old persisted runs that referenced a separate `modules`
  artifact will not re-install under the new format; acceptable per the greenfield "replace, don't
  deprecate" policy (no production data).

## Guardrails
`just lint` · `just type` (whole tree) · `just test` before each commit; full `just ci` before push.

## Live proof (challenge iter-1, finding 4)
This dev host runs KVM/libvirt directly, so the combined-tar build→install path is exercised live,
not deferred: run the `live_vm`-gated local build + install proof on this host before opening the PR
and record the build stamp + outcome in the PR body. If the live stack cannot be brought up in time,
that limitation is stated explicitly in the PR body rather than implied.
