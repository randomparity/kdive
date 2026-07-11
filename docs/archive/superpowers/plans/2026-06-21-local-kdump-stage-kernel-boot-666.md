# Local-libvirt kdump: stage the from-source kernel into the guest `/boot` (#666) — Implementation Plan

> **For agentic workers:** Implement task-by-task with TDD (`superpowers:test-driven-development`): failing test first, confirm it fails for the right reason, minimal implementation, focused test + guardrails green, refactor only while green. Steps use checkbox (`- [ ]`) syntax.

**Goal:** On the from-source KDUMP install lane, stage the already-staged from-source kernel image into the per-System qcow2 overlay at `/boot/vmlinuz-<ver>` (same rw libguestfs session that injects `/lib/modules/<ver>`), so the guest's `kdumpctl`/`kdump.service` finds a crash kernel to kexec-load and in-guest kdump actually arms.

**Architecture:** ADR-0206 already injects `/lib/modules/<ver>` via a `GuestModuleWriter` seam during `LocalLibvirtInstall.install`. This change renames that seam to `GuestKernelWriter`, extends its `inject` to also take the kernel image path, and has the real writer upload the kernel to `/boot/vmlinuz-<ver>` in the same mount. The kernel bytes are already fetched at install (`{staging}/{system_id}/{run_id}/kernel`) for the direct-kernel `<kernel>` element; no new artifact/fetch. Version `<ver>` is the modules-tarball release (`_read_release`). Gated identically to module injection (`method is KDUMP` and `modules_ref is not None`).

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`. libvirt-python, libguestfs (`guestfs`, live_vm only). Spec: [`../specs/2026-06-21-local-kdump-stage-kernel-boot-666.md`](../specs/2026-06-21-local-kdump-stage-kernel-boot-666.md). ADR: [ADR-0207](../../adr/0207-local-libvirt-stage-kernel-into-guest-boot.md).

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict, whole-tree (`just type`).
- ≤100 lines/function, cyclomatic complexity ≤8, ≤5 positional params, absolute imports only, Google-style docstrings on non-trivial public APIs.
- Pick the most specific existing `ErrorCategory` (`domain/errors.py`); never invent strings. A failed kernel upload / failed sentinel is an `INFRASTRUCTURE_FAILURE` carrying the overlay path (mirrors the modules `_io_failure`).
- Real libguestfs (`upload`/`mkdir_p`/`statns`) / `depmod` / `domain.destroy()` edges stay `# pragma: no cover - live_vm`, selected only in `from_env`. Pure orchestration is fake-tested — no host.
- CI runs `just lint`, `just type`, `just test`, plus the doc gates **individually** (not via `just ci`). Run the relevant ones before each commit; run the **full** `just ci` once before the first push.
- Conventional-commit subjects ≤72 chars; end every commit message with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Non-goals (do not touch):** the build plane, `InstallRequest`/ports, the MCP surface, DB schema/migrations, the upload (`initrd_ref`) lane, remote-libvirt, the guest image (`kdump.service` enablement is gap 2, out of scope).

---

## File structure

| File | Responsibility | Change |
|------|----------------|--------|
| `src/kdive/providers/local_libvirt/lifecycle/install.py` | local install plane | Rename `GuestModuleWriter`→`GuestKernelWriter`; `inject` gains `kernel_image: Path`; `_inject_built_modules` passes the staged kernel path; `_RealGuestModuleWriter.inject` uploads `/boot/vmlinuz-<ver>` + non-empty sentinel; add `upload`/`mkdir_p`/`statns` to the `_GuestFS` Protocol |
| `tests/providers/local_libvirt/test_install.py` | install unit tests | Update fake writer + existing injection tests to the new signature; add kernel-path assertion, sentinel test, version-derivation test |

No other files change (verify with a repo-wide grep for `GuestModuleWriter` — see Task 1).

---

## Task 1: Audit references to the renamed seam

**Where it fits:** Pre-flight so the rename is complete and nothing else imports the old name.

- [ ] `rg -n "GuestModuleWriter|_FakeModuleWriter|module_writer|\.inject\(" src tests` and list every site. Expected: the Protocol, `LocalLibvirtInstall.__init__` (`module_writer` param + `from_env` wiring), `_inject_built_modules`, `_RealGuestModuleWriter`, and the test fake. Confirm no other module imports `GuestModuleWriter`.

**Acceptance:** A written list of every call/definition site; no surprise importer outside `install.py` + `test_install.py`.

**Rollback:** none (read-only).

---

## Task 2 (TDD): Extend the seam contract — `inject` takes the kernel image

**Where it fits:** The protocol/orchestration change that lets the writer receive the kernel.

Files: `install.py`, `test_install.py`.

- [ ] **Test first.** Update `_FakeModuleWriter` (rename to `_FakeKernelWriter`) so `inject(self, overlay, kernel_image, modules_tar)` records the `kernel_image` it was handed. Extend `test_install_kdump_with_modules_ref_injects_and_no_initrd_rendered` to assert the writer received the per-Run staged kernel path (`…/{system_id}/{run_id}/kernel`) and that the path exists at inject time. Run; confirm it fails (signature mismatch / attribute absent) for the expected reason.
- [ ] **Implement.** Rename `GuestModuleWriter`→`GuestKernelWriter`; change the Protocol method to `inject(self, overlay: str, kernel_image: Path, modules_tar: Path) -> None`. Update `LocalLibvirtInstall.__init__` type hint (keep the `module_writer` attribute name **or** rename to `kernel_writer` consistently — pick one and update `from_env`). In `_inject_built_modules`, pass `kernel_path = staging_dir / "kernel"` (the file `install()` already fetched) to `self._<writer>.inject(overlay_path(system_id), kernel_path, modules_tar)`.
- [ ] **Guardrails.** `just lint && just type && uv run python -m pytest tests/providers/local_libvirt/test_install.py -q`. Green.

**Acceptance:** The KDUMP-lane install hands the writer the staged kernel path; ordering (force-off → fetch → inject) is preserved; non-kdump path still does not inject (existing test passes with the new fake signature). The kernel argument ordering in `inject` is documented.

**Rollback:** revert `install.py` + `test_install.py`.

---

## Task 3 (TDD): Real writer uploads `/boot/vmlinuz-<ver>` with a non-empty sentinel

**Where it fits:** The live-only edge that actually writes the kernel into the overlay.

Files: `install.py`, `test_install.py`.

- [ ] **Test first.** The real upload/`statns` calls are `live_vm`-gated, so the version→path wiring and the sentinel must be **extracted into pure helpers** and tested host-free (do not assert only `_read_release` — that already exists and proves nothing about the wiring):
  - **Kernel destination path** — add a pure helper `_kernel_dest(version: str) -> str` returning `f"/boot/vmlinuz-{version}"` that `inject` calls. Unit-test it directly (e.g. `_kernel_dest("7.0.0") == "/boot/vmlinuz-7.0.0"`) **and** that it composes with `_read_release`'s recovered `<ver>` (feed a `lib/modules/<ver>/…` tarball through `_read_release`, then through `_kernel_dest`, and assert the full `/boot/vmlinuz-<ver>` string) — so a typo like `vmlinux-` or a missing `-{version}` suffix is caught in CI.
  - **Non-empty sentinel** — add a pure helper (e.g. `_verify_kernel_size(size: int, overlay: str) -> None`) that raises `INFRASTRUCTURE_FAILURE` (overlay path in `details`) on size 0 and returns on size > 0. Unit-test both branches. Keep it pure so it is not `live_vm`-gated.
  Run; confirm failure for the expected reason.
- [ ] **Implement.** In `_RealGuestModuleWriter`: add `upload`, `mkdir_p`, `statns` to the `_GuestFS` Protocol. After `_extract_and_index`, within the same `inject` rw session: `guest.mkdir_p("/boot")`, `guest.upload(str(kernel_image), self._kernel_dest(version))`, then read the size via `statns` and call `_verify_kernel_size(size, overlay)`. Order: extract modules → depmod → modules.dep sentinel → mkdir/upload kernel → kernel-size sentinel. Annotate only the real I/O lines (`mkdir_p`/`upload`/`statns`) `# pragma: no cover - live_vm`; the two helpers stay pure (covered).
- [ ] **Guardrails.** `just lint && just type && uv run python -m pytest tests/providers/local_libvirt/test_install.py -q`. Green.

**Acceptance:** `<ver>` for the kernel filename comes from the modules tarball (one source); a zero-byte kernel upload is a typed `INFRASTRUCTURE_FAILURE` naming the overlay; a non-empty upload passes. Idempotency holds (upload truncates/creates; `mkdir_p` is idempotent).

**Rollback:** revert `install.py` + `test_install.py`.

---

## Task 4: Docstrings + module audit

**Where it fits:** Keep the module's docstrings honest about the new behavior (ADR-0207).

- [ ] Update the `_RealGuestModuleWriter` and `_inject_built_modules` docstrings to state they now also stage `/boot/vmlinuz-<ver>` (cite ADR-0207). Update the module header's `install()` summary if it enumerates the injection.
- [ ] Re-run the Task 1 grep to confirm no stale `GuestModuleWriter` references remain anywhere.

**Acceptance:** Docstrings cite ADR-0207 and describe kernel staging; no stale name; `just lint && just type` green.

**Rollback:** revert docstring edits.

---

## Task 5: Full suite + guardrails before push

- [ ] `just ci` (full PR gate). All green; in particular `just type` (whole-tree), `just test`, and the doc gates. Note any `live_vm`-skipped tests are expected (no KVM locally).

**Acceptance:** `just ci` green locally; the only skips are hardware-gated `live_vm`/`live_stack`.

**Rollback:** n/a (verification only).

---

## Verification matrix

| Concern | Covered by |
|---------|-----------|
| Writer is handed the staged kernel path (KDUMP lane) | Task 2 unit test |
| Force-off → fetch → inject ordering preserved | existing test, extended (Task 2) |
| Non-kdump System does not force-off/fetch/stage kernel | existing test, new fake signature (Task 2) |
| Kernel destination path derives from modules-tarball `<ver>` | Task 3 unit test (pure `_kernel_dest` composed with `_read_release`) |
| Zero-byte kernel upload → typed `INFRASTRUCTURE_FAILURE` | Task 3 unit test (pure `_verify_kernel_size`) |
| Real `upload`/`mkdir_p`/`statns` into overlay | `live_vm` / runbook (hardware) |
| Full panic→arm→capture→harvest arc | `live_vm` / runbook (hardware), gap-2 image precondition |
