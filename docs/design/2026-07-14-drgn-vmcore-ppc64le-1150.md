# Spec — Verify drgn vmcore analysis for ppc64le targets (#1150)

- **Date:** 2026-07-14
- **Issue:** #1150 · **Epic:** #1139 (full ppc64le support) · **Depends on:** #1148 (merged, PR#1169)
- **ADR:** [0348](../adr/0348-drgn-vmcore-ppc64le-verification.md)
- **Design:** `docs/design/2026-07-13-ppc64le-full-support.md` §Debug plane

## Problem

The debug plane's one cross-arch surface is the **offline vmcore-analysis path**: the
worker host opens a vmcore captured from a (possibly foreign-arch) guest and loads that
guest's `vmlinux`. drgn supports ppc64le targets upstream, and #1148 (ADR-0346) now
captures a real ppc64le vmcore under TCG — but the offline path has only ever been
exercised against x86_64 cores, and every one of its tests drives a fake whose
`uts().machine` is hardcoded `"x86_64"`. So neither "a ppc64le vmcore opens and yields the
same contract" nor "the offline path is arch-neutral by regression" is established.

This is **verification, not new machinery** (design doc §Debug plane): the audit in
ADR-0348 finds the offline path already arch-opaque. The work is to prove it — live for the
open, by arch-parameterized tests for the contract — and to fix whatever the live proof
surfaces.

## Scope

In scope:

- Arch-parameterize the offline drgn vmcore tests (`tests/providers/local_libvirt/
  test_introspect_drgn.py`, `tests/providers/debug_common/test_drgn_program.py`, and the
  remote mirror `tests/providers/remote_libvirt/debug/test_introspect.py`) so a ppc64le
  case (`machine="ppc64le"`) drives the same assertions as x86_64.
- Strengthen the catalog `drgn_version` assertion for the ppc64le row so it stays
  *meaningful* (clears the `live_drgn_capability` threshold), not merely snapshot-equal.
- A documented live drgn-open of the real #1148 ppc64le vmcore.
- Any production fix the live proof forces (fail-fast on evidence; none anticipated).

Out of scope (settled elsewhere in the epic):

- The drgn **live/SSH** path — arch-neutral by construction (design doc §Debug plane).
- Obtaining/shipping ppc64le `kernel-debuginfo` — the debuginfo requirement is arch-neutral
  (x86_64 needs it too) and orthogonal to the cross-arch question (ADR-0348 rejected
  alternative).
- Big-endian ppc64 (ADR-0347 scopes it out).
- gdb/MI arch selection (#1149, ADR-0347, already merged).

## Requirements & acceptance criteria

Mapping the issue's two acceptance criteria to falsifiable checks:

**AC1 — A ppc64le vmcore opens and yields the same introspection contract as x86_64
(task list, symbol reads).**

- **AC1a (live, mandatory):** On the x86_64 host, drgn opens the real #1148 ppc64le vmcore
  (`local/runs/<run>/vmcore-kdump`), `prog.platform.arch` reports the powerpc/ppc64
  architecture, and the core's VMCOREINFO `BUILD-ID=` line reads. Proves "a ppc64le vmcore
  opens" with no debuginfo. Recorded in a proof record under `docs/design/`.
- **AC1b (full contract, debuginfo-gated):** If a DWARF-bearing ppc64le `vmlinux` matching
  the captured kernel is obtainable on the proof host, the proof additionally drives
  `LocalLibvirtVmcoreIntrospect.from_vmcore` end-to-end and records that the task list,
  modules, and `sysinfo.machine == "ppc64le"` sections populate with the same shape as
  x86_64. If not obtainable, AC1b is carried by the arch-parameterized unit tests (below)
  and the proof record notes the arch-neutral debuginfo constraint. **UNVERIFIED only if
  drgn cannot open the real core at all** — never for lack of debuginfo.

**AC2 — Arch-parameterized tests in `tests/` mirror the existing x86_64 coverage.**

- `_FakeProgram` (and the remote/`test_drgn_program.py` fakes) gain an `arch` knob
  defaulting to `"x86_64"`, so all existing assertions remain byte-identical.
- The sysinfo/uts contract test and the `from_vmcore` happy-path test are parameterized
  over `{"x86_64", "ppc64le"}` and assert the **identical contract shape** — four sections
  (tasks/modules/sysinfo/truncated), same keys, same redaction and byte-cap behavior — with
  only `sysinfo.machine` differing by arch.
- The remote-libvirt mirror gets the same parameterization.
- A test ties the ppc64le catalog row's `drgn_version` to `live_drgn_capability`.
- `just ci` green (lint, type whole-tree, lint-shell, lint-workflows, check-mermaid, test).

## Approach

Per ADR-0348: **no production change** (the path carries no `x86_64` to remove). The
deliverable is test coverage + a live proof + the catalog assertion. The `arch` knob on the
fakes is the minimal seam — it threads only into `uts()`, since that is the sole
arch-observable value in the contract. Parameterizing over `{x86_64, ppc64le}` proves the
orchestration is arch-blind; the live open proves drgn reads a real ppc64le core.

## Risks & failure modes

- **Live proof surfaces a real defect** (e.g. drgn version too old for ppc64le, or a
  helper that silently assumes x86). *Mitigation:* fail-fast — fix the defect, add a
  regression test, record the finding in ADR-0348 and the proof record. Not anticipated:
  the adapter uses only arch-general drgn helpers.
- **The #1148 vmcore artifact is gone from the object store.** *Mitigation:* the artifact
  survives in `/home/dave/kdive-ppc-proof/` inputs and the live stack is up; if the object
  is expired, re-run the #1148 `live_stack` capture (`test_ppc64le_kdump_captures_a_vmcore_
  under_tcg`) to regenerate one, then open it. The proof records which path was taken.
- **Default-arg regression risk on the fakes.** *Mitigation:* the `arch` knob defaults to
  `"x86_64"`; the existing x86_64 assertions are the guard that the default is inert.
- **`platform.arch` API name drift across drgn versions.** *Mitigation:* the proof reads
  `prog.platform.arch` against the installed drgn 0.2.0 and records the exact value; the
  proof is a one-shot live artifact, not a CI test, so no version-pinning is needed.

## Non-goals / explicitly deferred

No migration, no schema change, no new dependency, no agent-facing tool-contract change.
The offline path is verified through the same code x86_64 already uses.
