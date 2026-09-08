# ppc64le live proof: banner scan, fadump and kdump capture (#2383)

## Problem

#2382 rewrote the ppc64le ELF banner scan so a release token cut by a read-chunk boundary
fails instead of registering a silent prefix. The PASSED evidence on #2312 ran against the
pre-fix scan and never crossed a boundary, so it does not cover `main`. #1204 separately
still owes an end-to-end fadump crash→capture proof. The fadump driver skips unless
`platform.machine() == "ppc64le"`, so no x86_64 workstation can produce that evidence, and
the native POWER host named in #1204 is not reachable from here.

## Scope

One unit: run the kdive live stack **inside** an operator-local emulated ppc64le guest
(QEMU `pseries`, `accel=tcg`, Fedora 44 ppc64le, checksum-verified). That guest is both the
ppc64le builder — `virt-customize --run-command` refuses a cross-arch guest, so the rootfs
fixture and kernel bundle can only be built on ppc64le — and the run host, where
`platform.machine()` satisfies the fadump gate natively and `expected_accel("ppc64le")`
resolves `tcg` with no `/dev/kvm`.

Provision the guest; build `KDIVE_GUEST_IMAGE_PPC64LE` and the `KDIVE_PPC64LE_BUNDLE` pair
(`--exclude='lib/modules/*/vmlinuz'`); confirm the checkout is merged `main`; `just stack-up`;
`just test-live-tcg`; write one dated proof record under `docs/design/`, host identifiers
replaced by stable tokens.

Excluded (operator-approved, `WORK:SCOPE q2383-4ac00b9e`): no `src/kdive/` change; the
2026-07-15 #1181 record is not reopened; the fadump platform gate is unchanged; no CI,
runner-role, or builder-harness change lands here.

## Success

1. The bundle validates and the persisted kernel release equals the guest's whole
   `uname -r`, not a prefix.
2. `test_ppc64le_fadump_captures_a_vmcore_under_tcg` passes rather than skips.
3. `test_ppc64le_kdump_captures_a_vmcore_under_tcg` passes.
4. One dated, redacted proof record lands under `docs/design/`; #1204 closes on it.

## Validation

All commands run inside the ppc64le guest, on this branch, whose `src/` and `tests/` are
byte-identical to merged `main` — the branch adds documentation only.

- **Persisted release completeness** (1) — Mode: `focused-test`.
  `tests/integration/test_live_stack.py::test_ppc64le_uploaded_kernel_bundle_boots_over_the_wire`,
  then read the release back off the Run. Red: a release shorter than the guest `uname -r`.
  Green: `uv run python -m pytest -m live_vm_tcg -k bundle -q`.
- **fadump crash→capture** (2) — Mode: `focused-test`.
  `...::test_ppc64le_fadump_captures_a_vmcore_under_tcg`. Red: reports `skipped` on x86_64,
  which is not capture evidence. Green: `uv run python -m pytest -m live_vm_tcg -k fadump -q`.
- **kdump capture and `raw_vmcore_refs` redaction** (3) — Mode: `focused-test`.
  `...::test_ppc64le_kdump_captures_a_vmcore_under_tcg`. Red: a raw `vmcore-kdump` ref
  surviving `raw_vmcore_refs`. Green: `uv run python -m pytest -m live_vm_tcg -k kdump -q`.
- **Proof record and the platform-support capture rows** (4) — Mode:
  `task-test-not-applicable`. The record narrates one historical run; nothing executable
  reads its content, and an assertion over it could only restate the run it reports, so no
  task-specific observation could fail meaningfully.
