# ADR 0348 — drgn vmcore analysis on ppc64le: trust drgn's arch-neutrality, lock it with tests, prove it live

- **Status:** Accepted
- **Date:** 2026-07-14
- **Issue:** #1150
- **Epic:** #1139 (full ppc64le support)
- **Builds on:** ADR-0344 (#1146 arch-opaque boot bundle: same "audit → trust → arch-param
  tests → live proof" shape), ADR-0346 (#1148 ppc64le kdump capture — produces the real
  ppc64le vmcore this issue analyzes), ADR-0343 (arch-aware upload contract),
  ADR-0033/0083 (offline vmcore introspection ports), ADR-0203 (shared drgn core-file
  helpers), ADR-0301 (drgn path for by-name field reads)

## Context

The vmcore-analysis path is the debug plane's one **cross-arch** surface. drgn's *live*
path runs in-guest over SSH (`debug/live_introspect.py`) and is arch-neutral by
construction — the guest's own drgn reads the guest's own kernel. The *offline* path is
different: the worker host opens a vmcore captured from a possibly-foreign-arch guest and
loads that guest's `vmlinux`. Until #1148 there was never a ppc64le vmcore to point it at.
#1148 (ADR-0346) now captures one under TCG, so the offline path can finally be verified.

Auditing the offline path shows it is **already arch-opaque**:

- `open_vmcore_program` (`shared/debug_common/drgn_program.py:147`) calls
  `drgn.Program().set_core_dump(core)` + `load_debug_info([vmlinux])`. drgn reads the
  target architecture from the core's ELF header / VMCOREINFO and the loaded DWARF; the
  Python code names no arch.
- `DrgnProgramAdapter` (`drgn_program.py:107-153`) reads only **arch-general** drgn
  helpers: `pid.for_each_task`, `module.for_each_module`, `cpumask.for_each_online_cpu`,
  and the fixed by-name symbols `init_uts_ns`, `saved_command_line`, `_totalram_pages`.
  None of these encode an ISA; drgn resolves struct layouts from the loaded DWARF, so the
  same calls decode a ppc64le core exactly as an x86_64 one.
- `read_vmcoreinfo_build_id` (`drgn_program.py:32`) matches the `BUILD-ID=` line in
  VMCOREINFO — a text note present on every ELF/kdump core regardless of arch.
- `LocalLibvirtVmcoreIntrospect.from_vmcore` (`local_libvirt/debug/introspect.py:99`) and
  its remote mirror do provenance (build-id match), staging, fixed-helper dispatch,
  redaction, and byte-capping — all byte/string operations, no arch branch.

ADR-0301's "by-name field reads" routes agent-supplied `struct->field` reads to the drgn
**script** path (live, over SSH), which the epic already treats as arch-neutral. The
offline path's own by-name reads are the *fixed* symbol set above, not agent-supplied, and
are covered here.

The single arch-observable value in the whole contract is the `sysinfo.machine` string
(the kernel's `init_uts_ns.name.machine`) — `"x86_64"` vs `"ppc64le"`. It flows through
untouched; the code neither validates nor branches on it.

Two things the audit does **not** settle:

- **Whether drgn genuinely opens a real ppc64le core on this host** (drgn's ppc64le support
  is upstream but unexercised here). This is a live fact, not a code-reading fact.
- **Whether the offline tests actually pin the arch-neutrality.** Every existing drgn
  vmcore test drives a `_FakeProgram` whose `uts()` hardcodes `machine="x86_64"`
  (`test_introspect_drgn.py:137`, asserted at `:261`; `test_drgn_program.py`), so a future
  regression that made the path x86-literal would not be caught.

The remaining x86-literalness is in **test fixtures and prose**, not production code — the
same tribal-knowledge trap ADR-0344 removed from the boot path.

## Decision

**The offline vmcore-analysis path stays arch-opaque and trusts drgn to interpret the core;
we add no arch-specific machinery. The verification is arch-parameterized regression tests
plus one documented live drgn-open of the real #1148 ppc64le vmcore.**

Concretely:

- **No production change.** The path carries no `x86_64` assumption to remove; adding a
  ppc64le branch would invent a second, drift-prone arch gate for bytes drgn already
  interprets. (If the live proof surfaces a real defect, fix *that* — fail-fast on the
  evidence — and record it here.)
- **Arch-parameterized regression tests are the durable guard.** `_FakeProgram` gains an
  `arch` knob (default `"x86_64"`, so every existing assertion stays byte-identical). The
  sysinfo/uts contract test and the full `from_vmcore` happy-path test are parameterized
  over `{x86_64, ppc64le}`, asserting the **identical contract shape** (four sections,
  same keys, same redaction/byte-cap behavior) with only `machine` differing. The remote
  mirror gets the same parameterization. They fail the instant a change makes the offline
  path branch on arch or drop a section for a non-x86 core.
- **Catalog `drgn_version` stays meaningful for the ppc64le row (row hygiene).** Beyond the
  existing snapshot equality (`test_rootfs_catalog.py:125`), a focused assertion checks the
  ppc64le row's `drgn_version` parses via `DrgnVersion.parse` (`images/drgn_support.py`) into
  a real version clearing the `BTF_CAPABLE_DRGN` (0.0.31) floor its purpose implies — so a
  placeholder, empty, or malformed value is caught. This is **catalog-row hygiene, not
  offline-path proof:** `drgn_version` is the **guest-baked** drgn consumed by the in-guest
  live/SSH path, not the worker-host offline drgn (0.2.0) this issue verifies — the two are
  different drgns, and the assertion claims nothing about the offline contract. It
  deliberately does **not** pin cross-arch row-equality with `fedora-kdive-ready-44`: Fedora
  ppc64le is a *secondary* arch that may legitimately lag primary packaging, so an equality
  invariant would risk a spurious CI failure on a real divergence.
- **Live proof — discriminating, pinned, and honestly scoped.** The real-bytes verification
  is a `live_vm`-gated test (not a one-shot doc artifact) that opens the retained real #1148
  ppc64le vmcore with drgn and asserts drgn identifies it as **ppc64le specifically**:
  `prog.platform.arch == drgn.Architecture.PPC64` **and**
  `PlatformFlags.IS_LITTLE_ENDIAN in prog.platform.flags` (the arch enum has no LE/BE variant,
  so the flag is what separates ppc64le from the out-of-scope big-endian ppc64), a readable
  VMCOREINFO `BUILD-ID=`, and a **SHA-256 equal to the pinned digest** so the guard provably
  runs against this exact #1148 artifact. It exercises drgn's real ppc64le ELF-header + note
  parsing and **needs no debuginfo**. It **skips only** when the fixture is unconfigured
  (`KDIVE_PPC64LE_VMCORE` unset) and **fails loudly** when the env is set but the file is
  missing/unreadable or its digest mismatches — so a skip is distinguishable from a pass and a
  mis-provisioned runner is a failure, not a silent "no core." **Durability is within the live
  suite, not CI:** the 86 MiB core cannot ship to CI, so — like every `live_vm` test here — the
  guard runs in `just test-live` on the host holding the retained core; a green PR does not
  assert this AC. The proof record names that host, the drgn version last verified (0.2.0), the
  core's digest + retained path, and a re-run-on-drgn-bump trigger, so re-exercise is a recorded
  procedure rather than unstated discipline. The pinned digest is authoritative **in the test
  constant** (the proof record holds a human-readable copy); if the core is lost and re-captured,
  the runbook is to recompute the SHA-256 and update both in one commit, and the digest-mismatch
  message distinguishes "just re-captured — re-pin" from "core swapped/corrupt."
- **The full structural read on real ppc64le bytes is DEFERRED, not faked.** Reading the
  task list and by-name symbols out of a real ppc64le core requires a DWARF-bearing
  `vmlinux`. The epic ships only stripped `vmlinuz` and no ppc64le `kernel-debuginfo` (a
  secondary-arch package), so — following ADR-0344, which put real ppc64le DWARF out of
  scope — this issue does **not** prove the structural decode on real ppc64le bytes and
  explicitly defers it (follow-up: obtain ppc64le `kernel-debuginfo`, drive `from_vmcore`
  end-to-end). The arch-parameterized unit fakes prove the offline orchestration is
  arch-blind — the ADR audit shows the adapter uses only arch-general drgn helpers — and
  are **not** claimed as proof of real DWARF decoding; conflating the two would let the
  headline contract ship certified while never running on real ppc64le bytes. The debuginfo
  prerequisite is itself arch-neutral (the x86_64 offline path needs the identical
  `load_debug_info` — see `core_file.py`'s `DMESG_UNAVAILABLE`, which fires on *any* arch
  when debuginfo is absent). **UNVERIFIED — a defect, not a deferral — applies only if the
  real-bytes open above fails**, never for lack of production debuginfo.

## Consequences

- The offline vmcore path analyzes a ppc64le core through the same, unchanged code x86_64
  already uses; an x86_64 core's behavior is byte-identical (asserted, not assumed).
- The path has exactly one arch-observable value (`sysinfo.machine`), and it is inert —
  no second arch gate to drift.
- The arch-parameterized tests lock the arch-neutral offline *orchestration*: a future
  change that re-adds an x86 assumption to `drgn_program.py` / `introspect.py` fails CI.
- **Precisely scoped:** the ppc64le core is proven drgn-*openable* on real bytes
  (`Architecture.PPC64` + VMCOREINFO) with a durable `live_vm` guard; the *structural* read
  (task list / by-name symbols from real DWARF) is **deferred** pending ppc64le
  `kernel-debuginfo`, tracked as a Known-limitation, not claimed as done. The epic's
  "drgn on ppc64le" item is *partially* retired — open proven, structural read deferred —
  and this ADR says so rather than over-claiming from fakes.
- No migration, no schema change, no new dependency, no agent-facing contract change.

## Rejected alternatives

- **Add a ppc64le branch / arch-validate the core in the offline path.** Rejected: drgn
  already interprets the core's arch from its header + DWARF; a Python-side arch check adds
  no safety (same trusted bytes) and re-introduces the arch-literalness this issue removes.
  One interpreter — drgn — owns arch.
- **Assert arch-neutrality only at the unit level (skip the live proof).** Rejected: drgn's
  ppc64le *offline* support is unexercised on this host; "a ppc64le vmcore opens" is a live
  fact the acceptance criteria require, and only a real open can retire it. Fakes prove the
  orchestration is arch-blind; they cannot prove drgn reads a real ppc64le core.
- **Block the live proof on obtaining ppc64le `kernel-debuginfo`.** Rejected: the
  debuginfo requirement is arch-neutral (x86_64 needs it too) and orthogonal to the
  cross-arch question. The open + platform-arch + VMCOREINFO path proves the ppc64le core
  is genuinely drgn-openable without it; gating the whole proof on a heavy, possibly
  unavailable debuginfo package would block a verification that is already discriminating.
- **Cross-capture a fresh ppc64le vmcore for this issue.** Rejected: #1148 already captured
  a real one through the production pipeline; reusing it exercises the exact artifact an
  operator's capture produces, with no second TCG capture run on the proof host.

## Rollout

Additive and backward compatible. No migration and no behavior change on the x86_64 vmcore
path (the change is a test `arch` knob + arch-parameterized cases + a catalog assertion + a
live proof). ppc64le is verified through the same offline introspection code x86_64 already
uses.
