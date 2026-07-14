# Implementation plan — direct-kernel-boot ppc64le kernel bundles (#1146)

Spec: `docs/design/2026-07-13-ppc64le-boot-bundle-1146.md` ·
ADR: `docs/adr/0344-ppc64le-boot-bundle-direct-kernel.md`

Branch: `feat/ppc64le-boot-bundle-1146` · Base: `main` ·
**No migration, no schema change, no new config.** The boot path is already arch-opaque; the
code delta is docstring prose + arch-parameterized tests + a documented live proof.

TDD throughout: write the failing (or arch-parameterized) test first, then the code. Commit per
task with a conventional message ending in the repo's `Co-Authored-By` trailer. Keep guardrails
green at each commit — `just lint` (ruff), `just type` (ty, whole tree), `just test`; run
`just ci` before push. No task here touches tool docstrings/`Field`s or generated docs, so
`just docs-check` is unaffected (the upload-contract agent surface already landed in #1145).

## Ground truth (verified this session)

- **Boot path is byte-agnostic.** `extract_boot_vmlinuz`
  (`src/kdive/providers/local_libvirt/lifecycle/boot/kernel_bundle.py:23`) copies the
  `boot/vmlinuz` tar member's bytes to `dest` via `write_staged_bytes` — no magic read.
  `repack_modules_subtree` (`kernel_bundle.py:57`) and `_read_release`
  (`guest_kernel_writer.py:172`) key on `lib/modules/<ver>/` member *names*.
  `_render_direct_kernel_xml` (`install.py:404`) sets `<kernel>`/`<initrd>`/`<cmdline>` on the
  existing domain. None branch on arch.
- **x86-literal prose to fix:** `kernel_bundle.py:27-28` — "libvirt's direct-kernel `<kernel>`
  element needs a raw **bzImage** path" (false for ppc64le). Adjacent x86-flavored *examples* in
  `install.py` docstrings ("a bzImage with an embedded initramfs") at the module docstring (line 10)
  and the `install()` docstring (lines 259-260) — generalize the wording, keep the initrd-optional
  case. Note: `_render_direct_kernel_xml`'s docstring (line 414) is **already** arch-neutral
  ("embedded-initramfs kernel") — no edit there.
- **No dedicated test file for `kernel_bundle.py`** — `extract_boot_vmlinuz` is exercised only
  implicitly through `tests/providers/local_libvirt/test_install.py`, whose helper
  `_combined_kernel_tar_bytes(*, with_modules=True, version=_MODULES_VERSION)`
  (test_install.py:66) writes `boot/vmlinuz` = `b"bzImage-bytes"` (line 72) and
  `_MODULES_VERSION = "6.9.0"` (line 57). `repack_modules_subtree` has direct tests at
  test_install.py:305-333.
- **Writer injection triggers** only when `request.method is CaptureMethod.KDUMP or
  request.debuginfo_ref is not None` (`install.py:339`, `_inject_modules_if_needed`). A plain
  boot injects nothing. `_RealGuestKernelWriter.inject` (`guest_kernel_writer.py:82`) order is
  `_extract_and_index` (runs `guest.command(["depmod","-a",version])` at line 133) → `_stage_kernel`
  → `_stage_vmlinux` (uploads + checks `size>0`, lines 159-169). A `depmod` fault collapses to one
  `INFRASTRUCTURE_FAILURE` carrying only `type(exc).__name__` (lines 134-137, 192-198).
- **Install cmdline override seam** for the proof token: `install_run(..., cmdline: str | None =
  None)` (`src/kdive/mcp/tools/lifecycle/runs/steps.py:65`) applies an optional boot-cmdline
  override (ADR-0299/#988), re-staging the domain `<cmdline>`. Passing a unique token here and
  reading it from the booted guest's `/proc/cmdline` is the discriminating live assertion.
  **Guard:** `install_run` rejects a cmdline carrying a platform-owned token
  (`platform_owned_cmdline_token` → `root=`/`console=`/`crashkernel=`) with
  `cmdline_overrides_platform_args`, and rejects a blank cmdline — so the proof token must be a
  benign non-platform key, e.g. `kdive_proof_token=<uuid>`, not `console=…`.
- **ppc64le live harness (from #1144)** in `tests/integration/test_live_stack.py`:
  `_ppc64le_reachability_preflight()` (line 801, gates on `qemu-system-ppc64` +
  `KDIVE_GUEST_IMAGE_PPC64LE` + stack/db), `test_ppc64le_guest_is_ssh_reachable_over_the_wire`
  (line 834), the `arch=ppc64le` provision profile factory, and `select_kernel_and_initrd`
  (`lifecycle/rootfs/baseline_kernel.py:41`) which yields the baseline kernel + `initramfs-<ver>.img`.
- **`kdive-ready` marker on `hvc0`** is emitted from the real root by the readiness unit
  (`images/families/_fedora_customize.py`, ADR-0342); its appearance proves initramfs
  unpack→root-mount→pivot on pseries.

## Tasks

### Task 1 — De-x86 the boot-path docstrings/comments (no behavior change)

**What / where the task fits:** Spec §1 / ADR-0344 "De-x86 the prose"; criterion 6. The install/
boot mechanics are arch-opaque but the prose asserts a bzImage-only `<kernel>`. Correct the prose
so a future reader meets the arch-opaque contract where the code lives — the tribal-knowledge fix.

**Files:** `src/kdive/providers/local_libvirt/lifecycle/boot/kernel_bundle.py` (the
`extract_boot_vmlinuz` docstring); `src/kdive/providers/local_libvirt/lifecycle/install.py`
(the "e.g. a bzImage with an embedded initramfs" example prose at the module docstring, line 10,
and the `install()` docstring, lines 259-260 — generalize to "an embedded-initramfs kernel").
`_render_direct_kernel_xml`'s docstring (line 414) is already arch-neutral — do not touch it.

**Do:** Rewrite `extract_boot_vmlinuz`'s docstring to state the `<kernel>` element needs a raw
kernel image extracted host-side — a bzImage on x86_64, an ELF `vmlinux` on ppc64le (powerpc has
no bzImage). Keep it factual and short; do not rename the function or the `boot/vmlinuz` member
(contract-stable, ADR-0344 rejected alternative). Generalize the `install.py` example wording
without deleting the initrd-optional case.

**Acceptance:** No behavior change (docstrings/comments only). `just lint`, `just type`, `just test`
green — the existing `kernel_bundle`/`install` tests pass unmodified. A reader of
`extract_boot_vmlinuz` no longer sees a bzImage-only claim. (Pure-prose task: no failing test to
write first; it is the enabling cleanup for Tasks 2-3.)

**Rollback:** revert the single commit; prose-only, no dependents.

### Task 2 — New unit tests: `extract_boot_vmlinuz` + repack/`_read_release`, both arches

**What / where the task fits:** Spec §2 / criteria 1, 2. The durable regression guard for the
byte-agnostic *host-side* path — fails the instant a bzImage assumption re-enters extraction.

**Files (new):** `tests/providers/local_libvirt/lifecycle/boot/test_kernel_bundle.py` (create the
`.../boot/` test dir + `__init__.py` if absent — mirror the package path). Reuse the tar-building
idiom from `tests/providers/local_libvirt/test_install.py:60-82` (`_tar_add`,
`_combined_kernel_tar_bytes`).

**Do (write tests first):**
1. `extract_boot_vmlinuz` round-trip, parameterized over boot-member bytes:
   - x86: a bzImage-shaped blob (`b"MZ" + ...`, or reuse `b"bzImage-bytes"`).
   - ppc64le: an ELF64-LE `EM_PPC64` blob — a `\x7fELF\x02\x01` header with `e_machine=21` at
     offset `0x12`, padded (mirror the magic constants from `build_artifacts/validation.py`:
     `_ELF64LE_PREFIX`, `_EM_PPC64_LE16`).
   Assert the extracted host file is **byte-identical** to the member in both cases, and that a
   `./boot/vmlinuz`-prefixed member (leading `./`) still resolves (covers `_tar_member_path`).
2. Missing-member and unreadable-tar paths raise `CategorizedError` `INFRASTRUCTURE_FAILURE`
   (byte-agnostic error contract — not arch-specific, but lock it).
3. `repack_modules_subtree` + `_read_release` at a ppc64le version: build a combined tar with
   `lib/modules/6.19.10-300.fc44.ppc64le/…`; assert repack returns `True`, the modules-only tar
   contains the subtree, and `_read_release(modules_tar, overlay)` returns the `.ppc64le` version
   string verbatim (the arch suffix is not stripped). Add the x86 counterpart if not already
   covered.

**Acceptance:** New tests fail if `extract_boot_vmlinuz` is changed to reject non-bzImage bytes.
`just test` green; ppc64le and x86 cases both pass. No `live_vm`/`live_stack` marker (pure host
tar I/O).

**Rollback:** delete the new test file/dir; no production code touched.

### Task 3 — Arch-parameterize the install-flow test through inject + render

**What / where the task fits:** Spec §2 / criterion 2 (orchestration). Exercises the whole
extract → repack → inject → `<kernel>`/`<initrd>`-render path for a ppc64le bundle with the
injected **fake** writer, proving the *orchestration* is arch-opaque. (Explicitly NOT the real
writer's in-guest depmod — that is Task 4's live question; call this out in a test-module comment
so a reader does not over-read the coverage.)

**Files:** `tests/providers/local_libvirt/test_install.py` — extend `_combined_kernel_tar_bytes`
to take an `arch` (or add a sibling helper) that emits an ELF `EM_PPC64` `boot/vmlinuz` and an
arch-suffixed module version; parameterize the relevant install/inject cases over
`{x86_64, ppc64le}`.

**Do (tests first):** Add a ppc64le parameter to the kdump/debuginfo inject cases (e.g. the
existing `test_install_kdump_injects_modules_from_combined_tar…`) and a plain-boot case, asserting:
the fake writer receives the ppc64le module tar (version `…​.ppc64le`); the staged `<kernel>`
file's bytes equal the ELF boot member (not the x86 bytes); when an initrd is staged the rendered
`<os>` carries `<initrd>`; and `<cmdline>` is passed through verbatim. Keep every existing x86
assertion byte-identical (criterion 1) — parameterize, don't rewrite.

**Acceptance:** `just test` green; the ppc64le params pass with the fake writer; x86 assertions
unchanged. Test-module comment states the fake-writer scope caveat (real-writer depmod is live-only).

**Rollback:** revert the test edits; no production code touched.

### Task 4 — Live proof (`live_stack`, TCG) + proof-record doc + ADR finding

**What / where the task fits:** Spec §3 / criteria 3, 4, 5 and ADR-0344's live-proof + writer +
initrd-finding bullets. This is the headline deliverable and the one non-unit task. It runs on the
x86_64 dev host (this host runs KVM/libvirt directly and has `qemu-system-ppc64`); it **skips
cleanly** on a host without the ppc64le emulator/image/stack, per the #1144 preflight idiom.

**Files:** `tests/integration/test_live_stack.py` (a new `@pytest.mark.live_stack`
`test_ppc64le_uploaded_kernel_bundle_boots_over_the_wire`, reusing
`_ppc64le_reachability_preflight` + the `arch=ppc64le` provision factory); a new proof record
`docs/design/2026-07-13-ppc64le-boot-bundle-proof-record-1146.md` (mirror
`2026-07-13-ppc64le-tcg-boot-proof-record-1144.md`); and, only if the live run forces it, a
narrowly-scoped pseries accommodation in code + a test (spec §3, ADR-0344).

**Do — the run (read the upload/complete_build seam live to wire it):**
1. Provision an `arch=ppc64le` System (admission persists `accel=tcg`), as #1144 does.
2. Package the guest's own baseline ppc64le kernel + its `initramfs-<ver>.img`
   (`select_kernel_and_initrd`) as the ADR-0343 combined tar (`boot/vmlinuz` = the ELF,
   `lib/modules/<ver>/`) plus a separate `initrd` artifact; upload both and `runs.complete_build`
   (validates the boot member as a ppc64le ELF, ADR-0343) to obtain `kernel_ref` + `initrd_ref`.
   Determine the exact upload wiring by reading `artifacts.expected_uploads` /
   `artifacts.create_system_upload` / `runs.complete_build` at proof time.
3. `runs.install` with a **unique cmdline token** (the `install_run` cmdline override,
   `mcp/tools/lifecycle/runs/steps.py:65`) → `runs.boot`. The token must be a benign non-platform
   key (e.g. `kdive_proof_token=<uuid>`) — `install_run` rejects `root=`/`console=`/`crashkernel=`
   and blank cmdlines.
4. **Assert + attribute (discriminating, criterion 3):** the running domain's `<kernel>`/`<initrd>`
   (via `virsh dumpxml`) resolve to the per-Run staged paths (`{staging}/{system_id}/{run_id}/…`);
   and the unique token appears in the guest's `/proc/cmdline` over SSH.
5. **Initrd verdict (criterion 4), pinned tokens on `hvc0`:** first confirm `hvc0` is teed from
   domain start and persisted on a non-ready boot (so "no output" ≠ "not captured"); then classify:
   `kdive-ready` on `hvc0` → "no addressing quirk," retire issue 7; kernel banner + an
   initramfs-stage failure token (`VFS: Unable to mount root fs` / `dracut:` FATAL / `Cannot open
   root device`) → "quirk," land the accommodation + test and attribute via the offline `virsh`
   per-Run-path check; anything else → indeterminate (do **not** retire), iterate.
6. **Writer verdict (criterion 5):** a second `runs.install` with a **stub** `debuginfo_ref`
   (`method != KDUMP`) to trigger `_RealGuestKernelWriter.inject` on the ppc64le overlay. The stub
   must be an **actually-uploaded, resolvable object-store artifact carrying non-empty bytes**:
   `_inject_built_modules` *fetches* `debuginfo_ref` (install.py:401) **before** `inject()`, and
   `_stage_vmlinux` size-checks it (`size>0`, guest_kernel_writer.py:169) — a made-up ref string
   raises `STALE_HANDLE` at fetch and depmod never runs, and a zero-length blob fails the size
   check *after* depmod. The bytes' arch is irrelevant (they are not executed; depmod runs first).
   If inject completes → writer verified. If `depmod` fails, read the chained `__cause__` /
   libguestfs log for `exec format error` / binfmt → record the cross-arch constraint, defer the
   `qemu-user`/`binfmt` accommodation to issue 9. **A fetch/resolve failure is a proof-wiring error
   to fix, NOT the UNVERIFIED case** — UNVERIFIED applies only if the writer's inject itself cannot
   run on the host (e.g. libguestfs absent), never because a ref was mis-supplied.

**Do — the record:** Write the proof-record doc with the console evidence (the `hvc0` capture),
the per-Run-path attribution, the initrd finding, and the writer verdict. Append the definitive
finding (initrd addressing + writer) to ADR-0344. The epic Known-unverified item (issue 7) is
already struck in the spec commit pending this proof; if the proof is indeterminate, the strike is
reverted (issue 7 not retired) — do not leave it struck on an indeterminate result.

**Acceptance (criteria 3-5):** The `live_stack` test passes on the dev host (or skips cleanly
elsewhere) and drives upload→install→boot of the uploaded bundle; the proof record captures a
definitive `hvc0` token and the per-Run-path attribution; ADR-0344 carries the initrd finding and
the writer verdict (verified, constrained-and-deferred, or — only if unrunnable — UNVERIFIED). If
the initrd verdict is indeterminate, the issue-7 strike is reverted and the blocker is surfaced,
not silently marked done. `just ci` green (the new `live_stack` test is gated behind the marker, so
CI's `just test` excludes it; the unit tasks carry the green gate).

**Rollback:** the `live_stack` test skips without the harness, so it cannot redden CI; the proof
doc and ADR appendix are additive. A forced pseries accommodation (if any) is a small, separately
revertable commit with its own test.

## Task ordering & prerequisites

1 → 2 → 3 → 4. Task 1 (prose) is the enabling cleanup; Tasks 2-3 (unit guards) gate CI green and
must land before the live proof so the byte-agnostic contract is locked first. Task 4 is last: it
consumes the same host-side path Tasks 2-3 guard and produces the live evidence + ADR finding. No
task depends on a migration, schema, or config change. Tasks 1-3 are context-free implementer-
ready; Task 4 requires the live host and is inherently iterative (its *outcome* — verified vs.
constrained-and-deferred vs. indeterminate — is bounded by the spec's falsifiable branches).
