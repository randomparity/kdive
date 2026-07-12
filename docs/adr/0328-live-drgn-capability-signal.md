# ADR 0328 — Record guest drgn version and compute a live-drgn introspection capability signal

- **Status:** Accepted
- **Date:** 2026-07-12
- **Issue:** #1091
- **Builds on:** ADR-0286 (image capability metadata), ADR-0253 (kdump capability predicate), ADR-0323 (operator-attested provenance), ADR-0322 (drgn-live missing-debuginfo warning)

## Context

An agent choosing a base image for live kernel introspection cannot tell, before it burns an
Allocation provisioning the image, whether the drgn shipped in that image can actually introspect
the kernel it will boot. In-guest drgn reads the running kernel's own BTF
(`/sys/kernel/btf/vmlinux`, from `CONFIG_DEBUG_INFO_BTF`) rather than uploaded DWARF (ADR-0322), so
the deciding factor is the drgn build in the image. drgn is installed **unpinned** from distro
repos (`drgn` on rhel via EPEL, `python3-drgn` on debian), so the shipped version varies sharply by
image family — Debian bookworm freezes at 0.0.22 while Fedora and current EPEL ship 0.0.31–0.1.0.
The path of least resistance (single-kernel debian/rocky/centos images) can land on a drgn too old
to introspect a live kernel from BTF alone.

The catalog already has the exact precedent to mirror: `makedumpfile_version` is a curated,
test-guarded per-image operand of a *computed* kdump-capability predicate (ADR-0253), and
`images.describe` surfaces the result through the ADR-0286 capability-signal framework. The
`live_drgn` signal was named but parked as a `PlannedSignal` ("not honestly computable yet") for
lack of an operand.

`BLACK_BOX_REVIEW.md` F1 recommendation 1 (verified) calls for closing this gap by giving the
signal a concrete operand.

## Decision

We will replicate the makedumpfile precedent for drgn.

1. **Operand.** Add a curated `drgn_version` field to the rootfs catalog schema
   (`RootfsCatalogEntry`) and populate it for every `rootfs_catalog.toml` row, verified against
   distro package indexes and guarded by `tests/images/test_rootfs_catalog.py` — exactly as
   `makedumpfile_version` is. It is a dated snapshot, not live upstream truth; the value read at
   runtime is `provenance["drgn_version"]`.

2. **Predicate.** Add a pure `kdive.images.drgn_support` module (mirroring `kdump_support`) with a
   totally-ordered `DrgnVersion`, a `BTF_CAPABLE_DRGN` threshold, and `live_drgn_capability(...)`.
   The predicate degrades to `unverified` (never a confident `capable`) when the operand is absent
   or unparseable, preserving the ADR-0286 honesty invariant.

3. **Signal.** Promote `live_drgn` from `PlannedSignal` to a registered `CapabilitySignal` reading
   `provenance["drgn_version"]` and gated on the `drgn` tooling tag, surfaced through the existing
   `images.describe` `data.capability_signals` block with the ADR-0323 `basis`
   (`build_verified` / `operator_attested`) on a present operand.

### BTF-capability threshold policy

`BTF_CAPABLE_DRGN = 0.0.31`. drgn's ability to debug a live kernel **without full DWARF** — the
kallsyms symbol index (0.0.30), ORC stack unwinding read directly from the core dump, and the
module API that backs BTF-based type/object finders (0.0.31) — reached practical usability at
0.0.31, the release drgn itself frames as the milestone toward "debugging the Linux kernel without
full DWARF debugging information." The threshold is a curated **lower bound and a policy floor**,
not upstream truth: it is a monotonic `>=` check (an older drgn never gains the capability) and may
be raised as drgn's BTF support matures. The drgn release highlights are the human reference,
carried in the predicate's `note` (mirroring the makedumpfile ChangeLog pointer). A row below the
threshold — currently only `debian-kdive-ready-12` (bookworm, 0.0.22) — computes `incapable`.

Unlike kdump, the answer is **not kernel-relative**: BTF lives in the booted guest's own
`/sys/kernel/btf`, so the signal takes no `target_kernel` operand (it accepts and ignores the
uniform signal-signature argument, as `direct_kernel` does).

## Consequences

- An agent reads `capability_signals.live_drgn` from `images.describe` before provisioning and can
  avoid an image whose drgn cannot introspect the kernel it boots — closing the F1 gap without a
  new tool or parameter.
- The curated `drgn_version` snapshot must be refreshed as distro repos move drgn, on the same
  cadence and guard as `makedumpfile_version`. The dated comment records when it was verified.
- The `live_drgn` planned-signal backlog entry is removed; `sysrq` remains the sole `PlannedSignal`.
- **Not in this change:** wiring build-time drgn-version capture into published-image provenance
  (the makedumpfile marker/probe pipeline in the families/provider build planes). The signal reads
  `provenance["drgn_version"]` and honestly degrades to `unverified` for a KDIVE-built image until
  that capture, or an operator attestation, records the operand — identical to makedumpfile's state
  for an externally-baked image. The curated catalog snapshot is the characterization of record.
  BBR F1 rec 3 (pin/refresh guest drgn at build time) stays a follow-up.

## Alternatives considered

- **Keep `live_drgn` as a `PlannedSignal`.** Leaves the F1 gap open; the operand precedent
  (makedumpfile) shows a curated, test-guarded version is a legitimate, honest operand.
- **A stored boolean `drgn_live_capable` bit.** Rejected for the same reason ADR-0253 rejected a
  stored kdump bit: a threshold that shifts as drgn evolves would drift a baked-in bit into a
  confident-but-wrong answer. Compute from the version against an explicit, revisable threshold.
- **Make the signal kernel-relative like kdump.** Wrong model: in-guest BTF is the booted guest's
  own, independent of any `target_kernel` the caller names — a kernel operand would imply a
  matrix that does not exist.
- **Set the threshold at 0.0.27 (pluggable finders) or 0.0.30 (kallsyms).** Those are enabling
  steps, not the point at which DWARFless kernel debugging was practically usable; 0.0.31 is the
  conservative, defensible floor and can be raised later.
- **Block on build-time provenance capture.** Would couple a read-side selection affordance to the
  live-only build/probe pipeline; the curated operand mirrors makedumpfile and ships value now,
  with capture as an orthogonal follow-up.
