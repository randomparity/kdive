# ADR 0316 — Compose multiple build-config fragments; guard rootfs symbols via the requirements seam

- **Status:** Accepted
- **Date:** 2026-07-08
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0096](0096-kdump-config-fragment-build-input.md) (the seeded kdump
  fragment and `config`-replaces-default resolution this extends), [ADR-0065](0065-provider-component-references.md)
  (the `ComponentRef` kinds and `ConfigRequirements` validator this reuses).
- **Spec:** [`../specs/2026-07-08-config-compose-list-1036.md`](../specs/2026-07-08-config-compose-list-1036.md)
- **Issue:** #1036 (epic #998; `BLACK_BOX_REVIEW.md` P5)

## Context

A server-build `config` ComponentRef **replaces** the seeded `kdump` fragment instead of composing
with it (`config = profile.config or DEFAULT_CONFIG_REF` at three resolve sites), and `config` is a
single ref — so an agent cannot add a fragment (e.g. fault-injection) while keeping kdump without
copying kdump's whole body into its own fragment (the black-box reviewer's exact workaround, P5).

`kdump.config` carries not just crash-dump/debug symbols but the rootfs/boot-critical symbols the
built kernel needs to mount the kdive squashfs+overlay image (`SQUASHFS`, `SQUASHFS_ZSTD`,
`OVERLAY_FS`, `BLK_DEV_LOOP`, `XFS_FS`). Nothing forces those to survive: `_dropped_fragment_symbols`
only checks symbols the supplied fragment names, and the always-on `REQUIRED_KERNEL_CONFIG` guards
only `CRASH_DUMP` + a debuginfo group. So a custom `config` that omits the rootfs symbols builds
clean and boots to a dead guest — a silent footgun that compose-support alone would not close.

The already-shipped config surface (#1032 `config_ref` echo, #1033 pre-flight, #1039 defconfig-layering
doc) and the requirements seam (ADR-0065 `ConfigRequirements` + `validate_config_requirements`, used
today only when a profile names `profile_requirements`) exist but were never pointed at compose or at
the rootfs invariant.

## Decision

1. **`config` composes as an ordered list.** `ServerBuildProfile.config` accepts a single
   `ComponentRef` **or** a non-empty `list[ComponentRef]`. A list concatenates its fragments in
   declaration order (later wins on conflict, matching `merge_config.sh -m`) and **fully replaces**
   the default — the agent lists `{catalog:kdump}` explicitly to keep kdump. `DEFAULT_CONFIG_REF` is
   still resolved only when `config` is absent (semantics unchanged). A single normalizer,
   `config_refs(profile)`, replaces the three `or DEFAULT_CONFIG_REF` idioms so the resolve and
   validate sites cannot diverge. An empty list is a `CONFIGURATION_ERROR`.

2. **A universal rootfs-mount guard, reusing the requirements validator.** A new constant
   `PLATFORM_REQUIRED_CONFIG` (the five exact `=y` rootfs-mount symbols) is validated at build against
   the post-`olddefconfig` `.config` in `_validate_final_config`, always, reusing
   `validate_config_requirements` and re-raising with `details.reason = "platform_config_symbol_missing"`.
   The **selection principle**: the universal set holds exactly the symbols every kdive System needs to
   mount its image regardless of capture method, which `olddefconfig` will not auto-select.
   Capture-method-specific symbols (`FW_CFG_SYSFS` for host_dump, #708) are excluded — they belong to
   the per-method `profile_requirements` seam, since forcing them on a gdbstub-/console-only build would
   over-guard it. The pre-existing `REQUIRED_KERNEL_CONFIG` check (`CONFIG_CRASH_DUMP` + debuginfo
   OR-group) is **retained unchanged** — not moved, weakened, or widened, so its `missing_any_of`
   failure shape is preserved. The final-config drop-check is made **net-intent-aware** so a later
   composed fragment that disables/downgrades an earlier symbol is honored, not flagged as a spurious
   drop.

3. **Surfaced as exactly what is enforced — a self-check aid, not a bootability proof.**
   `buildconfig.get` echoes `data.platform_required_config = {all_of, any_of}` derived from
   `PLATFORM_REQUIRED_CONFIG` and `REQUIRED_KERNEL_CONFIG`; the agent-facing `config` Field names the
   replace-not-compose semantics and points at that field. A test asserts the surfaced payload is built
   from the constants the guard validates against, so the discoverable set can never become a subset of
   the guarded one. A drift guard ties the constants to the seeded `kdump.config`, and a guard-passes
   test exercises `_validate_final_config` against a representative good final `.config` (fragment-text
   alone would not prove `olddefconfig` survival). The guard runs at build, not at
   `runs.validate_profile` (which only parses), so the surfaced set is a self-check aid — not a
   pre-flightable guarantee — and it certifies mount, not full kdump-functional completeness.

Scope: server-build lane only — the `.config`-text guard is not *applicable* to the external/`complete`
lane (no `olddefconfig`, no server-side `.config`). The unbootable-guest failure mode still exists
there for a prebuilt kernel missing the symbols; this ADR does not claim to close it (a possible
follow-up surfaces the requirement from `IKCONFIG` at upload). No DB migration — the profile persists
in the `runs.build_profile` JSONB and a list value round-trips; existing single-ref/absent profiles
parse unchanged.

## Consequences

- An agent composes `config: [{catalog:kdump}, {catalog:faultinject}]` without copying kdump.
- Forgetting the rootfs symbols is now a fail-fast `CONFIGURATION_ERROR` naming them at build time,
  not a silently dead guest — on both the single-fragment and compose paths.
- One requirements model spans profile requirements and the platform invariant; the guard is
  discoverable through the same surface the agent uses to pick fragments.
- The default (absent `config`) and existing single-ref builds are byte-for-byte unchanged.
- New failure detail `reason = "platform_config_symbol_missing"` for a missing rootfs-mount symbol (a
  `CONFIGURATION_ERROR` variant, not a new `ErrorCategory`). The pre-existing `CONFIG_CRASH_DUMP` /
  debuginfo check is unchanged, keeping its `missing_any_of` shape.
- The guard certifies rootfs *mount*, not full kdump-functional completeness; capture-method symbols
  stay in `profile_requirements`. The `config` list is bounded (`MAX_CONFIG_FRAGMENTS`) so an unbounded
  compose cannot open unbounded per-ref catalog fetches.

## Considered & rejected

- **kdump as an always-included base with an opt-out flag.** Removes the forget-kdump footgun by
  construction but adds a request knob and shifts `DEFAULT_CONFIG_REF` from "default when absent" to
  "always-on base" — more magic across three sites, and the reviewer's own example lists kdump
  explicitly, signalling they expect to own the base. The list-replaces model plus the always-on
  rootfs guard closes the same footgun without the semantic shift.
- **Extend the hard-coded `REQUIRED_KERNEL_CONFIG` tuple with the rootfs symbols.** Minimal diff, but
  a second ad-hoc guard divorced from the `ConfigRequirements` mechanism and invisible to the agent as
  "required." Reusing the requirements seam keeps enforcement and discovery on one model.
- **Compose only; split the rootfs guard to a follow-up.** Leaves the silent-unbootable failure mode
  in place while actively making it easier to hit (compose invites custom fragment sets). The guard is
  what makes compose safe, so they ship together.
- **Merge fragments at the `local` component-path or add new ref kinds.** Out of scope; a list is an
  ordered set of refs each resolved by the unchanged `resolve_config_bytes` rules.
- **Guarding every kdump-functional symbol (`FW_CFG_SYSFS`, `KEXEC_*`, `IKCONFIG`, …) in the universal
  set.** Over-guards debug workflows that don't use a given capture method, and turns the always-on set
  into an unprincipled grab-bag. Method-specific requirements belong to `profile_requirements`; the
  universal guard stays scoped to mount.
