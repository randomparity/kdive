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

- **AC1a (real-bytes open — mandatory, live-suite durable).** A `live_vm`-gated test opens
  the retained real #1148 ppc64le vmcore with drgn and asserts, on real bytes with **no
  debuginfo**, that drgn identifies it as **ppc64le specifically**:
  - `prog.platform.arch == drgn.Architecture.PPC64` (exact enum, confirmed on-host in drgn
    0.2.0), **and** `PlatformFlags.IS_LITTLE_ENDIAN in prog.platform.flags` — the arch enum
    has no LE/BE variant, so the endianness flag is what discriminates ppc64**le** from the
    out-of-scope big-endian ppc64 (both share `Architecture.PPC64`);
  - the core's VMCOREINFO `BUILD-ID=` line reads (exercises real ppc64le note parsing).
    *Empirically confirmed on the retained core* — it carries a parseable
    `BUILD-ID=06466f9617cff9e5a762af9216bfc23837310b9c`, so `read_vmcoreinfo_build_id`
    (which raises `CONFIGURATION_ERROR` on absence) returns rather than raises; the assertion
    is safe to make mandatory because the property was checked on this artifact, not assumed;
  - the file's SHA-256 equals the pinned digest
    `bd322c68c540542484cde32df94d3e074874374a1eb2ca50551e808f4c7190fa` **and** its size equals
    the pinned `90463884` bytes — two independent anchors, so the guard provably runs against
    *this exact #1148 artifact*, not a swapped/truncated core, and a truncated core is caught
    even if the digest were mis-pinned. The size independently corroborates #1148's own record
    (its proof record logs the captured core at 90463884 bytes), tying the pin to the
    artifact's birth record rather than resting on a single spec-author computation.

  **Skip vs. fail discipline (a skip must be distinguishable from a pass):** the test skips
  **only** when the fixture is unconfigured (`KDIVE_PPC64LE_VMCORE` unset). When the env is
  set but the file is missing, unreadable, or its digest mismatches, it **fails loudly** —
  a mis-provisioned runner is a failure, not a silent "no core." **Honest durability scope:**
  the 86 MiB core cannot ship to CI, so — like every `live_vm` test in this repo — this guard
  runs in `just test-live` on a host that holds the retained core, **not** in PR CI. Its
  durability is therefore *within the live suite*: a green PR does **not** assert this AC. The
  proof record names the host holding the core, the drgn version last verified against it
  (0.2.0), and a **re-run-on-drgn-bump trigger**, so the guard's re-exercise is a recorded
  procedure, not unstated discipline. A one-shot proof record under `docs/design/` captures
  the run with the core's digest and retained path.
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
- A test asserts the ppc64le row's `drgn_version` stays meaningful as **catalog-row
  hygiene**: it parses via `DrgnVersion.parse` (`images/drgn_support.py`) into a real
  version — so a placeholder, empty, or malformed value is caught — and clears the same
  `BTF_CAPABLE_DRGN` (0.0.31) floor the row's stated purpose implies. *Framing correction
  from review:* `drgn_version` is the **guest-baked** drgn (consumed by the in-guest live/SSH
  path), not the worker-host offline drgn (0.2.0), so this is row hygiene, **not** offline-path
  proof; the assertion makes no claim about the offline contract. It deliberately avoids a
  strict cross-arch equality with `fedora-kdive-ready-44`: Fedora ppc64le is a *secondary*
  arch that can legitimately lag primary-arch packaging, so pinning row-equality would risk a
  spurious CI failure on a real divergence. The existing snapshot test
  (`test_rootfs_catalog.py:125`) already pins the exact `0.0.33` value; this adds the
  parseability/floor hygiene.
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
  this (they never invoke real drgn), and — being 86 MiB — the core cannot ship to CI, so a
  green PR does **not** cover this AC. *Mitigation (honest scope):* AC1a lives in
  `just test-live` on the host holding the retained core; the proof record names that host,
  the drgn version last verified (0.2.0), and a re-run-on-drgn-bump trigger, so re-exercise
  is a recorded procedure. This is durability *within the live suite*, not a CI guarantee —
  the spec states that plainly rather than overclaiming.
- **A skipped guard is mistaken for a passing one.** *Mitigation:* the test skips **only**
  when `KDIVE_PPC64LE_VMCORE` is unset; when it is set but the file is missing/unreadable or
  its SHA-256 mismatches the pinned digest, the test **fails loudly** — a mis-provisioned
  runner cannot masquerade as "no core," and the pinned-digest assertion ties the bytes
  tested to the recorded artifact.
- **The #1148 vmcore artifact is lost / not reproducible.** A fresh TCG re-capture is slow
  and need not reproduce the same bytes — and because AC1a asserts digest equality, a
  re-captured core would fail the guard exactly as a corrupt one does. *Mitigation:* the core
  is retained at a stable path with its SHA-256 (`bd322c68…`) recorded; the test reads it from
  `KDIVE_PPC64LE_VMCORE`. **The pin lives in one authoritative place — the `live_vm` test
  constant — and the proof record carries a human-readable copy.** If the artifact is lost,
  the recovery runbook is: re-capture via the #1148 `live_stack` test
  (`test_ppc64le_kdump_captures_a_vmcore_under_tcg`), recompute the new core's SHA-256 **and size**, and
  **update both pins in the test (authoritative) and the proof-record copy** in one commit. The
  digest-mismatch failure message must distinguish the two cases — *"unexpected digest: if you
  just re-captured the core, recompute and update the pinned constant; otherwise the core at
  this path is swapped or corrupt"* — so a recovering operator is never left guessing whether
  a mismatch means "re-pin" or "corruption."
- **Default-arg regression risk on the fakes.** *Mitigation:* the `arch` knob defaults to
  `"x86_64"`; the existing x86_64 assertions are the guard that the default is inert.
- **`Architecture.PPC64` API drift across drgn versions.** *Mitigation:* AC1a asserts equality
  against the named enum member (confirmed present in the installed drgn 0.2.0); a drgn that
  renamed or dropped it would fail the `live_vm` test loudly rather than silently pass.

## Non-goals / explicitly deferred

No migration, no schema change, no new dependency, no agent-facing tool-contract change.
The offline path is verified through the same code x86_64 already uses.
