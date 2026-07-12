# ADR 0330 — Warn (not refuse) at complete_build when the uploaded config lacks boot-required symbols

- **Status:** Accepted
- **Date:** 2026-07-12
- **Issue:** #1095
- **Builds on:** ADR-0318 (debug-feature config gate), ADR-0322 (drgn-live missing_debuginfo warning), ADR-0316 (spec 1 removed kernel-config validation)

## Context

`runs.complete_build` accepts an uploaded `effective_config` artifact, stores it verbatim, and
drives the Run to `succeeded`. Spec 1 (ADR-0316) deliberately removed all kernel-config
validation from the build lane: the finalizer inspects the kernel tar and any `vmlinux` build-id
but never reads a single `CONFIG_*` symbol out of the config. A kernel whose config lacks the
symbols the guest needs to mount its root filesystem and boot (`EXT4_FS`, `VIRTIO_BLK` — the real
direct-kernel ext4 boot set corrected in #1094) is therefore accepted, completed, and then
silently fails to boot with no signal at upload time.

ADR-0318 built the `advertised`/`gate_required` split: `rootfs_mount` is advertise-only (never in
`gate_required`), so no seam refuses on it. ADR-0322 then established the pattern for the missing
signal we want here — a **non-fatal, fail-open warning** computed from the same
`load_effective_config` reader and spread into the response `data`, rather than a refusal.

The `rootfs_mount` symbols are boot-critical, but hard-gating `complete_build` on them would
reverse the spec-1 contract ("kdive never validates a `.config`") and could reject an in-flight
upload flow that boots fine for reasons kdive cannot see (a different rootfs driver, an initramfs,
an out-of-tree config). The issue offers gate-vs-warn; the boot set is advisory precisely because
kdive does not model every boot path.

## Decision

We will surface a **non-fatal `missing_boot_config` warning** on the `runs.complete_build`
success envelope, and we will **warn, not refuse**. A `rootfs_mount_warning(conn, run_id)` helper
in `kernel_config/gate.py` reuses the ADR-0318 fail-open `load_effective_config`: it returns a
`{reason, missing, remediation}` payload — or `None` — that the complete_build MCP handler spreads
into its response `data.missing_boot_config`, mirroring the ADR-0322 `missing_debuginfo`
convention rather than adding a new top-level `ToolResponse` field.

The warning fires only when an uploaded `effective_config` is present, non-degenerate, and
provably fails to enable one of the `rootfs_mount` **advertised** clauses (currently `EXT4_FS` and
`VIRTIO_BLK`). Absent, unreadable, or degenerate config completes exactly as today (fail-open, no
warning). The upload always succeeds and the Run always reaches `succeeded`; when the warning
fires, the envelope's `suggested_next_actions` lead with `artifacts.feature_config_requirements`
so the agent can see the missing symbols. The warning is computed on both the fresh-completion and
the replayed-completion paths, so a re-`complete_build` on a config-deficient Run keeps warning.

The check keys on the `rootfs_mount` advertise clauses, not `gate_required` (which is empty for
this feature): warn-not-refuse means a false warning (a boot path kdive cannot see) is harmless,
while a missed warning (silent boot failure) is the failure the signal exists to close. This
amends ADR-0318, which left `rootfs_mount` advertise-only with no consumer.

## Consequences

- An agent completing a build whose config provably lacks the ext4 direct-kernel boot symbols now
  gets a loud, symbol-naming `missing_boot_config` warning and a pointer to
  `artifacts.feature_config_requirements`, instead of a clean `succeeded` followed by an
  unexplained boot failure.
- The spec-1 contract is preserved: `complete_build` still never rejects over a `.config`, the
  config is still stored verbatim, and `validate_external_artifacts` still inspects no `CONFIG_*`
  symbol. The inspection lives entirely in the advisory warning path.
- kdive reads the `SENSITIVE` `effective_config` on the complete_build path too; only derived
  booleans and public `CONFIG_*` names leave the seam, never config bytes — the same boundary as
  the crash and debuginfo gates.
- `complete_build` pays one extra fail-open config read when a config was uploaded (no config →
  no S3 read: the DB key lookup short-circuits). This is negligible beside the finalize work and
  avoids a caching/staleness surface.
- The warning is advisory and heuristic: kdive does not verify the uploaded config against the
  kernel that actually boots, so at worst it warns for a boot path it cannot model — it never
  blocks a completion.

## Alternatives considered

- **Hard-gate / reject at complete_build.** Reverses the spec-1 "kdive never validates a
  `.config`" contract and can break an existing flow that boots via a path kdive cannot see
  (alternate rootfs driver, initramfs, out-of-tree config). The issue explicitly offers warn as the
  non-breaking option.
- **Add `rootfs_mount` to `gate_required` and reuse `unmet_clauses`.** Conflates "warn about this"
  with "the gate refuses on this"; the crash/install seams share that registry and would begin
  refusing. The warn path is a standalone advertise-clause check that never touches the refusal
  set.
- **Warn at boot instead of at complete_build.** The upload is the earliest actionable moment and
  the point the agent is already interacting with; a boot-time warning arrives after the wasted
  provision/boot and is harder to tie back to the config the agent uploaded.
- **A new typed `warnings` field on `ToolResponse`.** The envelope is `extra="forbid"` and is
  round-tripped by the compact-response middleware; a new top-level field is broader surface than
  the established `data`-payload convention ADR-0322 already uses.
- **Warn even on a degenerate/unreadable config.** Would turn every truncated or non-config upload
  into a boot-config warning; fail-open (arm/complete as today) keeps the signal precise to a real
  config that provably lacks the symbols.
