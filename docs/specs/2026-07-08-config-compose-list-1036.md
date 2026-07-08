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
2. **Platform-required config guard (reuse the requirements seam).** The build's *whole* always-on
   platform requirement — the rootfs/boot symbols (`SQUASHFS`/`SQUASHFS_ZSTD`/`OVERLAY_FS`/
   `BLK_DEV_LOOP`/`XFS_FS`), plus the symbols `REQUIRED_KERNEL_CONFIG` already enforces today
   (`CONFIG_CRASH_DUMP` exact, and the debuginfo OR-group `DWARF4|DWARF5|BTF`) — is validated at build
   against the final `.config` on the server-build lane **regardless of which `config` was supplied**,
   raising a `CONFIGURATION_ERROR` that names the missing symbols. This reuses the existing ADR-0065
   `validate_config_requirements` path (exact `=y` requirements) plus the existing `missing_config_groups`
   OR-group check, not a second ad-hoc mechanism.
3. **Surface exactly what is enforced.** The full platform requirement is discoverable by the agent
   before it builds — echoed in the `buildconfig.get` response and named in the agent-facing `config`
   Field text (the wrapper docstring/Field being the only contract the agent reads). The surfaced
   payload is **derived from the same constants the guard enforces**, so an agent that satisfies the
   shown set cannot then fail an unshown one — a test-guarded "surfaced == enforced" invariant.
4. **Back-compatible.** An existing single-ref or absent `config` behaves byte-for-byte as today. No
   DB migration (the profile persists in the `runs.build_profile` JSONB; a list value round-trips).

## Non-goals

- **`kdump`-always-included-base / opt-out flag.** Rejected in ADR-0316 — a list replaces the default;
  the agent owns the base. No `DEFAULT_CONFIG_REF` semantic shift.
- **Composing at the `local` component-path layer or across ref kinds beyond `catalog`/`local`.** The
  existing `resolve_config_bytes` ref-kind rules are unchanged; a list is just an ordered set of refs
  each resolved by the current rules.
- **Extending the guard to the external/`complete` (prebuilt-upload) lane.** The `.config`-text guard
  is not *applicable* there — that lane runs no `olddefconfig` and has no server-side `.config` to
  validate. The unbootable-guest *failure mode* still exists there (a prebuilt kernel missing
  `OVERLAY_FS`/`SQUASHFS` boots to the same dead guest); this issue does not address or claim to close
  it. Surfacing the same requirement from prebuilt-kernel metadata (e.g. `IKCONFIG`) at upload is a
  possible follow-up, not in scope here.
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
  `fragment_bytes`, which flows through the existing single-blob `checkout` seam to `merge_config.sh`.
  Order is significant: the merged `.config` must reflect **last-writer-wins per symbol** across the
  ordered fragments. This is a property of *our* merge step, not a borrowed cross-file guarantee — the
  design concatenates into one blob, so it is pinned by a test that merges a two-fragment blob whose
  later fragment redefines an earlier symbol and asserts the later value in the resulting `.config`.
  If that test cannot be made green with a single blob, the fallback is to pass each fragment as a
  separate `merge_config.sh -m` file argument (a `checkout`-seam signature change), which matches
  merge_config.sh's documented cross-file last-wins directly.
- **Validate (run creation):** `server_build` and `composite` iterate `config_refs(parsed)`, running
  the existing `reject_unsupported_component_source` and `config_validator` per ref (fail on the first
  bad ref). `runs.validate_profile` today only parses the profile and checks build-host/source-kind
  compatibility — it holds no provider `config_validator` seam — so for a list `config` it gains only
  what parsing provides: the union shape and the **non-empty-list rejection** (a `CONFIGURATION_ERROR`
  from the pydantic model). It does **not** gain per-ref source-capability checking; that stays at
  `runs.build`, and giving `validate_profile` that seam is out of scope.
- **Final-config check (net-intent, not raw union):** `_dropped_fragment_symbols` currently extracts
  *requested* symbols only from `=y`/`=m` lines (`common.py:_fragment_requests`), skipping
  `# CONFIG_X is not set` / `=n`. Fed the raw concatenated union that is wrong under last-wins: a later
  fragment that intentionally disables an earlier `=y` leaves a stale `=y` in the request map, so the
  final `.config` (correctly off) is flagged as a spurious "dropped by olddefconfig" error — breaking
  the very override the compose feature sells. The fix computes a **net effective-request map** over
  the ordered union: process every line in order, last write per symbol wins across all line kinds
  (`=y`/`=m`/`=n`/`# ... is not set`), and the drop-check then only asserts survival for symbols whose
  *net* intent is ON. A single fragment reduces to today's behavior.

### Platform-required config guard

Two constants are the single source of truth for the *entire* always-on requirement, so the surfaced
payload and the build guard are the same data:

```
# exact `=y` requirements: rootfs/boot symbols + crash-dump (moved out of REQUIRED_KERNEL_CONFIG)
PLATFORM_REQUIRED_CONFIG = ConfigRequirements(required={
    "CONFIG_SQUASHFS": "y", "CONFIG_SQUASHFS_ZSTD": "y", "CONFIG_OVERLAY_FS": "y",
    "CONFIG_BLK_DEV_LOOP": "y", "CONFIG_XFS_FS": "y", "CONFIG_CRASH_DUMP": "y",
})
# genuine OR-groups (any-of): debuginfo. REQUIRED_KERNEL_CONFIG reduces to just this.
REQUIRED_KERNEL_CONFIG = (("CONFIG_DEBUG_INFO_DWARF4", "CONFIG_DEBUG_INFO_DWARF5", "CONFIG_DEBUG_INFO_BTF"),)
```

`CONFIG_CRASH_DUMP` moves from `REQUIRED_KERNEL_CONFIG` (where it was a one-element OR-group) into the
exact set — behavior-equivalent (`=y` still required), but now it lives with the other exact
requirements so one dict covers all exact symbols and `REQUIRED_KERNEL_CONFIG` holds only the true
OR-group.

- **Enforced** in `_validate_final_config`, always (independent of `profile.profile_requirements`):
  `validate_config_requirements(config_text, PLATFORM_REQUIRED_CONFIG)` for the exact set, re-raising
  its `CONFIGURATION_ERROR` with `details.reason = "platform_config_symbol_missing"` and the `missing`
  symbols; plus the existing `missing_config_groups(config_text, REQUIRED_KERNEL_CONFIG)` for the
  debuginfo OR-group. Both were already enforced in substance — this reorganizes them into one
  surfaceable contract without weakening or widening enforcement.
- **Surfaced** as `data.platform_required_config` on `buildconfig.get`, a structured object derived
  from the two constants: `{"all_of": PLATFORM_REQUIRED_CONFIG.required, "any_of": [list(g) for g in
  REQUIRED_KERNEL_CONFIG]}`. The `config` Field names the replace-not-compose semantics and points at
  this field rather than re-enumerating, so the static text cannot drift from the constants.
- **`surfaced == enforced` invariant, test-guarded:** a test asserts the surfaced payload is built
  from the exact constants `_validate_final_config` validates against — so the discoverable contract
  can never become a subset of what the build enforces.
- **Guard proven against a real final `.config`, not fragment text:** the guard validates the
  *post-`olddefconfig`* `.config`, so a test that merely checks `kdump.config`'s bytes would not prove
  the default build passes (an unmet dependency could still drop a symbol at `olddefconfig`). Two
  tests instead: (a) a **drift guard** asserting every `PLATFORM_REQUIRED_CONFIG`/`REQUIRED_KERNEL_CONFIG`
  symbol is *declared* in the seeded `kdump.config` (so the constants cannot diverge from the seeded
  default); and (b) a **guard-passes test** running `_validate_final_config` against the existing
  representative good final-`.config` fixture the orchestration tests already use, asserting it passes.
  Survival of the symbols through a real `make olddefconfig` remains covered by the live/integration
  build path (out of band), which this spec does not duplicate.

### Why this is the smaller, safer change

The guard turns "silently unbootable" into a fail-fast `CONFIGURATION_ERROR` at build, and it does so
through the requirements mechanism the agent can already discover — not a hidden always-merged base
(more magic, changes `DEFAULT_CONFIG_REF` semantics) and not a second hard-coded tuple divorced from
the surfaced requirements. Compose stays a pure extension of the profile shape; the default path is
untouched.

## Acceptance criteria

- A `ServerBuildProfile` with `config` as a non-empty list of catalog refs parses; each ref is
  validated per-ref at `runs.build`/`runs.build_install_boot`; an empty list is rejected as
  `CONFIGURATION_ERROR` at parse, and that rejection is reachable through `runs.validate_profile`.
- On the server-build lane, a list config resolves to the ordered concatenation of the fragments'
  bytes merged onto defconfig, with **last-writer-wins per symbol** in the resulting `.config` — proven
  by a test that merges a two-fragment blob whose later fragment redefines an earlier symbol.
- A two-fragment compose whose later fragment **disables or downgrades** an earlier `=y` symbol builds
  successfully (the net-intent drop-check does not flag the intentional override).
- A single-ref or absent `config` produces byte-identical resolved bytes and validation behavior to
  the pre-change code (regression-guarded).
- A build whose final `.config` omits any exact platform symbol fails with `CONFIGURATION_ERROR`,
  `details.reason = "platform_config_symbol_missing"`, and the missing symbol(s) listed — on both the
  single-fragment and compose paths; a build missing the debuginfo OR-group still fails via the
  existing group check.
- `buildconfig.get` returns `data.platform_required_config` as `{all_of, any_of}`, and a test asserts
  that payload is derived from the exact constants `_validate_final_config` enforces (`surfaced ==
  enforced`).
- Drift guard: every surfaced platform symbol is declared in the seeded `kdump.config`. Guard-passes
  test: `_validate_final_config` accepts the existing good final-`.config` fixture.
- The `config` Field text names the replace-not-compose semantics and points at
  `data.platform_required_config`; generated `docs/guide/reference/*.md` regenerate clean via
  `just docs`.
- No DB migration. `just ci` green.

## Guardrails

`just lint`, `just type` (whole tree), `just test`, `just docs` / `just docs-check` (Field/docstring
changes regenerate reference docs), then `just ci`. Live-VM boot proof is out of band; the guard is a
pure-text `.config` check exercised by unit tests.
