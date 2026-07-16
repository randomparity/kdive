# ADR 0361 — Gate vmcore.fetch (kdump family) on the computed image kdump capability

- **Status:** Accepted
- **Date:** 2026-07-15
- **Deciders:** kdive maintainers

## Context

`vmcore.fetch` admits a `capture_vmcore` job against a `CRASHED` System (`_fetch_vmcore`,
`src/kdive/mcp/tools/lifecycle/vmcore/handlers.py`). For a kdump-family method
(`KDUMP`/`FADUMP`) the guest kernel produces `/proc/vmcore` and the in-guest `makedumpfile`
filters it into a usable core. Whether the booted rootfs image ships a `makedumpfile` new
enough for the crashing kernel is exactly what the ADR-0286/#957 computed-signal framework
answers (`kdive.images.cataloging.capability_signals.render_kdump_signal` over
`kdive.images.kdump_support.kdump_capability`, ADR-0253) — but that signal is consumed **only**
by `images.describe` catalog rendering. Nothing on the admission path reads it.

`_fetch_vmcore` already has two kdump gates: a provider-descriptor capability check (ADR-0209)
and `crash_capture_refusal` (ADR-0318), which inspects the Run's **uploaded** kernel `.config`
and fails **open** when no config was uploaded — precisely the catalog-image case. So a
`KDUMP` capture on `fedora-kdive-ready-43` (an image whose shipped `makedumpfile` is too old
for a v7.0-class kernel) is admitted today, and the operation fails opaquely deep in the
worker instead of fast at admission with an actionable next step.

## Decision

Add a third kdump-family admission gate to `_fetch_vmcore`: refuse a kdump/fadump
`vmcore.fetch` when the booted rootfs image's **computed** kdump capability is confidently
negative. Resolution is System `provisioning_profile` → local-libvirt `rootfs` → catalog
`ImageCatalogEntry`; the capability is `render_kdump_signal(entry, basis)` reused verbatim.

- **Fail open on uncertainty.** Refuse **only** when the computed status is `incapable`
  (the image's `makedumpfile` is provably too old for the kernel basis) or `not_applicable`
  (the image carries no `kdump` tooling tag — a kernel-independent, fully confident negative).
  Every other outcome passes: an unresolvable/unparsable profile, a non-local-libvirt provider,
  a non-`catalog` rootfs (`local`/`artifact`/`upload`), no visible registered catalog row, and
  a `capable`/`unverified` signal (missing or unparsable `makedumpfile_version`, or a kernel
  outside the characterized range). This mirrors the ADR-0318 gate's fail-open posture: a gate
  that cannot prove the negative never blocks.

- **Kernel basis: the characterized default (`DEFAULT_KERNEL_BASIS`).** The booted kernel
  version is **not** persisted in a machine-readable form at admission time. `kernel_source_ref`
  is documented (ADR-0078/0080) as an arbitrary provenance label with no valid-value set;
  `Run.kernel_ref` is an object-store key for the combined kernel+modules tar, and the release
  string lives only inside the archive. So the gate computes against
  `kdump_support.DEFAULT_KERNEL_BASIS` — the same basis `images.describe` shows by default —
  and leans on `kdump_capability`'s own honesty: a `makedumpfile` older than the characterized
  floor is `incapable`, anything the matrix cannot characterize degrades to `unverified` and
  passes. When a booted-kernel-version operand is later persisted, the gate should key on it;
  until then the characterized basis is the conservative, motivating-case-correct choice
  (`fedora-kdive-ready-43` → `incapable` at v7.0, exactly the reported case).

- **Method scope: the whole kdump family.** The gate keys on `KDUMP_FAMILY`
  (`KDUMP` + `FADUMP`), the same set the ADR-0318 gate uses: fadump reuses the kdump userspace
  and `makedumpfile` retrieve path (ADR-0349), so the makedumpfile-vs-kernel constraint applies
  identically. `HOST_DUMP` (host-side QEMU) and `GDBSTUB` never reach this branch.

- **Envelope: `configuration_error` with a distinct reason + `images.describe` next action.**
  An image that cannot serve kdump for its kernel is a caller/configuration mismatch, not a
  runtime dependency fault — the same taxonomy choice ADR-0209 made for
  `capability_unsupported`. A new closed-vocabulary reason `ConfigErrorReason.KDUMP_INCAPABLE`
  (`src/kdive/mcp/tools/_common.py`) rides in `data.reason`; the full computed capability block
  (status, `makedumpfile_version`, `min_makedumpfile_required`, `target_kernel`, note) rides in
  `data.kdump_capability` so the refusal discloses exactly why; `suggested_next_actions =
  ["images.describe"]` points the agent at the tool that renders the same signal. No job row is
  created on refusal. No DB migration (a pure admission-gating change).

## Consequences

- A kdump/fadump `vmcore.fetch` on a confidently kdump-incapable catalog image fails fast at
  admission with an actionable reason, instead of burning a worker job on a doomed capture.
- The gate is honest by construction: it can only *refuse* on a confident negative and *passes*
  on every uncertainty, so it never blocks a capture that might have worked. The residual
  tradeoff is the kernel basis — an image with an old `makedumpfile` booting an
  older-than-characterized kernel is not refused (`unverified`), which is the correct fail-open
  direction.
- `render_kdump_signal` now has a second consumer; the ADR-0286 honesty invariant (degrade to
  `unverified` on an absent operand) is what makes it safe to consume on the admission path.

### Rejected alternatives

- **A new `ErrorCategory` enum value.** The failure is a configuration mismatch already covered
  by `configuration_error` + a machine-readable reason (ADR-0174/0209); a new top-level category
  adds wire surface for no discrimination a reason token cannot carry.
- **Refuse only on `not_applicable`.** Kernel-independent and fully safe, but it leaves the
  reported motivating case (`incapable` — makedumpfile too old) unsolved.
- **Parse a kernel version from `kernel_source_ref` / open the kernel tar at admission.** The
  ref is a freeform label (unreliable); opening the tar is I/O on the request path for a version
  string the matrix only reads at `major.minor` granularity. The characterized basis is honest
  and cheap.
- **Gate `KDUMP` only, excluding `FADUMP`.** fadump shares the makedumpfile retrieve path
  (ADR-0349); excluding it would admit the identical doomed capture under the pseries variant.
