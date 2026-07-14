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

**AC1 — A ppc64le vmcore opens and drgn identifies it as ppc64le on real bytes.**

The issue's headline "yields the same introspection contract (task list, symbol reads)"
splits into two tiers by what real ppc64le bytes can actually prove on this host. The
distinction is load-bearing: an arch-parameterized *fake* whose `uts()` returns
`machine="ppc64le"` proves the orchestration is **arch-blind**, but invokes **zero** real
drgn ppc64le decoding (DWARF struct layout, pointer width, little-endian reads). So the
fakes cannot substitute for a real open; they and the real open verify different things.

- **AC1a (real-bytes open — mandatory, durable).** A `live_vm`-gated test opens the retained
  real #1148 ppc64le vmcore with drgn on the x86_64 host and **asserts
  `prog.platform.arch == drgn.Architecture.PPC64`** (the exact enum, confirmed present in
  drgn 0.2.0) and that the core's VMCOREINFO `BUILD-ID=` line reads. This exercises drgn's
  real ppc64le ELF-header + note parsing and **needs no debuginfo**. The test *fails* if the
  platform arch is anything other than `PPC64` or the build-id does not read; it **skips
  cleanly** when the core fixture is absent (env `KDIVE_PPC64LE_VMCORE` unset / missing).
  Being in the `live_vm` suite, it re-runs on drgn version bumps — so a future drgn that
  regresses real ppc64le-core opening is caught, not silently lost (see Risks). A one-shot
  proof record under `docs/design/` captures the same run with the core's SHA-256 digest.
- **AC1b (full structural contract — DEFERRED, debuginfo-gated).** Reading the *task list*
  and *by-name symbols* out of a real ppc64le core requires a DWARF-bearing `vmlinux`. The
  epic ships only stripped `vmlinuz` boot images and no ppc64le `kernel-debuginfo` (a
  secondary-arch package), so the full structural decode on **real** ppc64le bytes is **not
  proven by this issue** and is explicitly **deferred** — the same real-DWARF scope
  ADR-0344 put out of bounds. The arch-parameterized unit tests below prove the offline
  orchestration is arch-blind (the ADR audit shows the adapter uses only arch-general drgn
  helpers), which is what CI *can* guard; they are **not** claimed as proof of real ppc64le
  DWARF decoding. This deferral is recorded as a Known-limitation in the proof record and
  ADR-0348, with the follow-up being "obtain ppc64le `kernel-debuginfo` and drive
  `from_vmcore` end-to-end." **UNVERIFIED (a defect, not a deferral) applies only if AC1a's
  real-bytes open fails** — never for lack of debuginfo.

**AC2 — Arch-parameterized tests in `tests/` mirror the existing x86_64 coverage.**

- `_FakeProgram` (and the remote/`test_drgn_program.py` fakes) gain an `arch` knob
  defaulting to `"x86_64"`, so all existing assertions remain byte-identical.
- The sysinfo/uts contract test and the `from_vmcore` happy-path test are parameterized
  over `{"x86_64", "ppc64le"}` and assert the **identical contract shape** — four sections
  (tasks/modules/sysinfo/truncated), same keys, same redaction and byte-cap behavior — with
  only `sysinfo.machine` differing by arch.
- The remote-libvirt mirror gets the same parameterization.
- A test asserts the ppc64le row's `drgn_version` stays meaningful for the **offline**
  path: it is a non-empty, parseable version **equal to the same-distro/version x86_64 row**
  (`fedora-kdive-ready-44`), encoding the catalog's stated "Fedora 44 ships the same drgn
  across arches" invariant — so a placeholder or silently-degraded ppc64le drgn is caught.
  It does **not** tie to `live_drgn_capability`: that predicate is the in-guest BTF
  threshold (0.0.31) for the live/SSH path this issue puts out of scope, and against the
  installed drgn 0.2.0 it is near-tautological — the wrong capability for the offline
  vmcore contract.
- `just ci` green (lint, type whole-tree, lint-shell, lint-workflows, check-mermaid, test).

## Approach

Per ADR-0348: **no production change** (the path carries no `x86_64` to remove). The
deliverable is test coverage + a durable live-open test + a one-shot proof record + the
catalog assertion. The `arch` knob on the fakes is the minimal seam — it threads only into
`uts()`, since that is the sole arch-observable value in the contract. Parameterizing over
`{x86_64, ppc64le}` proves the orchestration is arch-blind; the `live_vm`-gated
`Architecture.PPC64` test proves drgn reads a **real** ppc64le core and is the durable
regression guard, not a one-shot artifact.

## Risks & failure modes

- **Live proof surfaces a real defect** (e.g. drgn version too old for ppc64le, or a
  helper that silently assumes x86). *Mitigation:* fail-fast — fix the defect, add a
  regression test, record the finding in ADR-0348 and the proof record. Not anticipated:
  the adapter uses only arch-general drgn helpers.
- **Regression: a future drgn stops opening ppc64le cores.** The unit fakes cannot catch
  this (they never invoke real drgn). *Mitigation:* AC1a is a `live_vm`-gated test, not a
  one-shot doc proof — it re-runs in `just test-live` against the retained core and fails on
  a real ppc64le-open regression. The proof record names the drgn-version-bump re-proof
  trigger explicitly.
- **The #1148 vmcore artifact is lost / not reproducible.** A fresh TCG re-capture is slow
  and need not reproduce the same bytes. *Mitigation:* the core is **retained at a stable
  path with its SHA-256 recorded** in the proof record, and the `live_vm` test reads it from
  `KDIVE_PPC64LE_VMCORE` (skipping cleanly when unset). Re-capture via the #1148
  `live_stack` test (`test_ppc64le_kdump_captures_a_vmcore_under_tcg`) is the last resort,
  and the proof record states which core (by digest) the run used.
- **Default-arg regression risk on the fakes.** *Mitigation:* the `arch` knob defaults to
  `"x86_64"`; the existing x86_64 assertions are the guard that the default is inert.
- **`Architecture.PPC64` API drift across drgn versions.** *Mitigation:* AC1a asserts equality
  against the named enum member (confirmed present in the installed drgn 0.2.0); a drgn that
  renamed or dropped it would fail the `live_vm` test loudly rather than silently pass.

## Non-goals / explicitly deferred

No migration, no schema change, no new dependency, no agent-facing tool-contract change.
The offline path is verified through the same code x86_64 already uses.
