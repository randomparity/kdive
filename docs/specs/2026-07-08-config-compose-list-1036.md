# Spec: compose multiple build-config fragments + platform-required rootfs guard (#1036)

- Issue: #1036 (parent epic #998; `BLACK_BOX_REVIEW.md` P5, un-tracked half)
- ADR: [ADR-0316](../adr/0316-compose-build-config-fragments.md)
- Relates: #1039 (config Field defconfig-layering doc, shipped), #1032/#1033 (config discovery + pre-flight), ADR-0096 (default kdump fragment), ADR-0065 (component refs + `ConfigRequirements`)
- Status: Draft

## Problem

A server-build `config` ComponentRef *replaces* the seeded `kdump` fragment rather than composing
with it. At all three resolve sites the gate is `config_ref = profile.config or DEFAULT_CONFIG_REF`
(`providers/shared/build_host/orchestration.py:108`, `mcp/tools/lifecycle/runs/server_build.py:84`,
`mcp/tools/lifecycle/runs/composite.py:118`): naming any `config` drops `DEFAULT_CONFIG_REF` (the
kdump catalog ref) entirely, and only that one fragment's bytes merge onto defconfig. `config` is a
single ref (`profiles/build.py:115`), so there is no way to add a fragment (e.g. fault-injection
symbols) *and* keep kdump — the black-box reviewer had to copy the whole kdump fragment into theirs.

Two distinct defects follow:

1. **No compose.** The reviewer asked for `config: [{catalog:kdump}, {catalog:faultinject}]`. The
   shape does not exist.
2. **Silent unbootability.** `kdump.config` carries the rootfs/boot-critical symbols the built
   kernel needs to mount the kdive squashfs+overlay image (`CONFIG_SQUASHFS`, `CONFIG_SQUASHFS_ZSTD`,
   `CONFIG_OVERLAY_FS`, `CONFIG_BLK_DEV_LOOP`, `CONFIG_XFS_FS` — `build_configs/data/kdump.config`).
   A custom `config` that omits them **passes both build checks** and yields an image that boots to a
   dead guest: `_dropped_fragment_symbols` only checks symbols the supplied fragment itself names
   (vacuous when they are simply absent), and the always-on `REQUIRED_KERNEL_CONFIG`
   (`orchestration.py:29`) guards only `CONFIG_CRASH_DUMP` + a debuginfo OR-group — **not** the rootfs
   symbols. So compose alone does not force kdump; forgetting it is still a silent footgun.

The already-shipped config surface (#1032/#1033/#1039) tells the agent what kdump contains, echoes a
`config_ref`, pre-flights a profile, and documents that a named `config` layers onto defconfig — but
every part of it still describes **one fragment that replaces the default**. #1036 is the compose gap
plus the rootfs guard that makes composing safe.

## Goals

1. **Compose.** `ServerBuildProfile.config` accepts a single `ComponentRef` (as today) **or** a
   non-empty `list[ComponentRef]`. A list concatenates fragments in declaration order and **fully
   replaces** the default — kdump is not auto-added; the agent lists `{catalog:kdump}` explicitly to
   keep it. `DEFAULT_CONFIG_REF` semantics are unchanged: it is resolved only when `config` is absent.
2. **Platform-required rootfs guard (reuse the requirements seam).** The rootfs/boot-critical symbols
   become a single-sourced platform-mandated `ConfigRequirements` set, validated at build against the
   final `.config` on the server-build lane **regardless of which `config` was supplied**, raising a
   `CONFIGURATION_ERROR` that names the missing symbols. This reuses the existing ADR-0065
   `validate_config_requirements` path, not a second ad-hoc guard.
3. **Surface it.** The platform-required set is discoverable by the agent before it builds: echoed in
   the `buildconfig.get` response and named in the agent-facing `config` Field text (the wrapper
   docstring/Field being the only contract the agent reads).
4. **Back-compatible.** An existing single-ref or absent `config` behaves byte-for-byte as today. No
   DB migration (the profile persists in the `runs.build_profile` JSONB; a list value round-trips).

## Non-goals

- **`kdump`-always-included-base / opt-out flag.** Rejected in ADR-0316 — a list replaces the default;
  the agent owns the base. No `DEFAULT_CONFIG_REF` semantic shift.
- **Composing at the `local` component-path layer or across ref kinds beyond `catalog`/`local`.** The
  existing `resolve_config_bytes` ref-kind rules are unchanged; a list is just an ordered set of refs
  each resolved by the current rules.
- **Extending the guard to the external/`complete` (prebuilt-upload) lane.** That lane runs no
  `olddefconfig` and has no server-side `.config` to validate; the footgun is specific to server
  builds. Out of scope.
- **Sanitizer/fault-injection fragment *seeding*** (#916/#917) — this issue adds the compose
  mechanism they depend on, not the fragments.

## Design

### Compose shape

`ServerBuildProfile.config: ComponentRef | list[ComponentRef] | None = None`, with a non-empty
constraint on the list form (an empty list is a `CONFIGURATION_ERROR` at parse — it is neither
"absent" nor a valid compose). A single source-of-truth normalizer replaces the three scattered
`or DEFAULT_CONFIG_REF` idioms:

```
def config_refs(profile: ServerBuildProfile) -> list[ComponentRef]:
    if profile.config is None:
        return [DEFAULT_CONFIG_REF]
    if isinstance(profile.config, list):
        return list(profile.config)
    return [profile.config]
```

- **Resolve (execution):** `orchestration.build_workspace` resolves each ref via the unchanged
  `resolve_config_bytes` and concatenates the bytes in order with a `\n` separator into
  `fragment_bytes`. Order is significant: `merge_config.sh -m` applies fragments left-to-right, so a
  later fragment's value wins on a conflicting symbol — documented, matching kconfig semantics.
- **Validate (run creation):** `server_build` and `composite` iterate `config_refs(parsed)`, running
  the existing `reject_unsupported_component_source` and `config_validator` per ref (fail on the
  first bad ref). `runs.validate_profile` pre-flights the same way.
- **Final-config check:** `_validate_final_config` receives the concatenated union text, so
  `_dropped_fragment_symbols` checks the whole union survived `olddefconfig` — unchanged logic, wider
  input.

### Platform-required rootfs guard

A single constant is the source of truth:

```
PLATFORM_ROOTFS_REQUIRED = ConfigRequirements(required={
    "CONFIG_SQUASHFS": "y", "CONFIG_SQUASHFS_ZSTD": "y",
    "CONFIG_OVERLAY_FS": "y", "CONFIG_BLK_DEV_LOOP": "y", "CONFIG_XFS_FS": "y",
})
```

- **Enforced** in `_validate_final_config`, always (independent of `profile.profile_requirements`),
  by reusing `validate_config_requirements`, re-raising its `CONFIGURATION_ERROR` with
  `details.reason = "platform_rootfs_symbol_missing"` and the `missing` symbols so the message is
  distinct from a profile-requirements failure.
- **Consistency-guarded** by a test asserting the seeded `kdump.config` satisfies
  `PLATFORM_ROOTFS_REQUIRED` — so the default build always passes, and if `kdump.config` ever drops a
  rootfs symbol the two are forced to move together (the constant cannot silently diverge from the
  seeded default).
- **Surfaced** as `data.platform_required_config` (the `.required` dict) in the `buildconfig.get`
  response, and named in the `config` Field text with a pointer to it — the enumeration lives once in
  the constant; the static Field text points rather than re-lists, so it cannot drift.

### Why this is the smaller, safer change

The guard turns "silently unbootable" into a fail-fast `CONFIGURATION_ERROR` at build, and it does so
through the requirements mechanism the agent can already discover — not a hidden always-merged base
(more magic, changes `DEFAULT_CONFIG_REF` semantics) and not a second hard-coded tuple divorced from
the surfaced requirements. Compose stays a pure extension of the profile shape; the default path is
untouched.

## Acceptance criteria

- A `ServerBuildProfile` with `config` as a list of catalog refs parses; each ref is validated at run
  creation; an empty list is rejected as `CONFIGURATION_ERROR`.
- On the server-build lane, a list config resolves to the ordered concatenation of the fragments'
  bytes, merged onto defconfig; later fragments override earlier on conflicting symbols.
- A single-ref or absent `config` produces byte-identical resolved bytes and validation behavior to
  the pre-change code (regression-guarded).
- A build whose final `.config` omits any `PLATFORM_ROOTFS_REQUIRED` symbol fails with
  `CONFIGURATION_ERROR`, `details.reason = "platform_rootfs_symbol_missing"`, and the missing
  symbol(s) listed — on both the single-fragment and compose paths.
- The seeded `kdump.config` satisfies `PLATFORM_ROOTFS_REQUIRED` (consistency test).
- `buildconfig.get` returns `data.platform_required_config`; the `config` Field text names the
  platform-required set and the replace-not-compose semantics; generated `docs/guide/reference/*.md`
  regenerate clean via `just docs`.
- No DB migration. `just ci` green.

## Guardrails

`just lint`, `just type` (whole tree), `just test`, `just docs` / `just docs-check` (Field/docstring
changes regenerate reference docs), then `just ci`. Live-VM boot proof is out of band; the guard is a
pure-text `.config` check exercised by unit tests.
