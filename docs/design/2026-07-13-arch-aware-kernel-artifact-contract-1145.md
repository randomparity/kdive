# Arch-aware kernel-artifact upload contract — bzImage vs ELF payload (#1145)

Date: 2026-07-13
Status: approved (design)
Issue: #1145 · Epic: #1139 (full ppc64le support) · ADR: `docs/adr/0343-arch-aware-kernel-artifact-contract.md`
Depends on: — (independent of the provisioning-seam subs; part of the epic spine)

## Problem

`build_artifacts/validation.py` is x86-literal. The combined `kernel` upload is a
gzip tar whose `boot/vmlinuz` member must carry the x86 **bzImage** `HdrS` magic at offset
`0x202`; both the validator (`_member_is_bzimage`, `_verify_combined_tar_shape`) and the
advertised contract (`EXTERNAL_BUILD_CONTRACTS["kernel"]`) say, in so many words, "the
bzImage, **NOT** the vmlinux ELF." powerpc has no bzImage: the bootable ppc64le kernel is an
**ELF** (`vmlinux`), which is also what Fedora/RHEL install as `/boot/vmlinuz-<ver>` on
ppc64le and what SLOF boots via `-kernel`. A valid ppc64le kernel upload is rejected today
with `kernel combined tar has no boot/vmlinuz bzImage member`.

The distinction cannot be inferred from the payload. The x86 rule is deliberately strict —
bzImage, not an ELF — so a validator that accepted "bzImage OR any ELF" would silently pass
an x86 `vmlinux` misplaced into the boot slot, defeating the original guard. The validator
must be told which arch it is validating so it can enforce exactly one format per arch.

The build lane is **decoupled** (ADR-0169): `runs.complete_build` can finalize an
**unbound** Run, so there is no provisioning-profile arch to rely on at validation time. The
arch must ride a document present on every build Run — the `build_profile`.

## Inputs (already landed)

- ADR-0234 (external-build upload contract): the `kernel`/`vmlinux`/`initrd`/
  `effective_config` artifact set, the combined-tar layout, and the
  `EXTERNAL_BUILD_CONTRACTS` advertisement whose byte details derive from the validator's
  own constants so the advertisement cannot drift.
- `domain/platform/arch_traits.py`: `SUPPORTED_ARCHES = {x86_64, ppc64le}` — the single
  source of truth for the arches kdive provisions.
- `extract_build_id_ranged` already requires **ELF64-LE** (magic, `EI_CLASS=2`,
  `EI_DATA=1`); ppc64le `vmlinux` is 64-bit little-endian, so the optional-`vmlinux`
  build-id path already fits ppc64le — only tests need an arch dimension.

## Design

### 1. Arch declaration on the build profile

`BuildProfile` (`profiles/build.py`) gains one field:

```python
arch: str = "x86_64"
```

validated at parse to be a member of `arch_traits.SUPPORTED_ARCHES`. It is optional and
defaults to `x86_64`, so every existing `{"schema_version": 1}` build profile stays valid
and x86 behavior is byte-identical. `schema_version` stays `1` — an additive optional field
with a default is a backward-compatible v1 document. Validation reuses the existing
`BuildProfile.parse` boundary, which already maps a Pydantic `ValidationError` to
`CONFIGURATION_ERROR` and scrubs submitted values; an unknown arch is rejected at
`runs.create`, **before** any upload round-trip.

`SUPPORTED_ARCHES` stays the single source of truth (a `pydantic.field_validator`, not a
duplicated `Literal`), so a future arch is one `arch_traits` row. The `Field` description
names the allowed values and the default for agent discoverability.

`dump_build_profile` serializes `arch` verbatim; the Run's persisted `build_profile` jsonb
carries it. No migration — the column is opaque jsonb and an absent `arch` re-reads as
`x86_64` via the model default.

### 2. Arch-keyed payload validation

A single per-arch table in `validation.py` is the source both the validator and the
advertisement read:

```python
BOOT_MEMBER_FORMATS: Mapping[str, FormatContract] = {
    "x86_64": FormatContract(container="bzImage",
                             magic=(MagicPin(offset=0x202, hex=_BZIMAGE_MAGIC.hex()),)),
    "ppc64le": FormatContract(container="ELF (vmlinux)", magic=(
        MagicPin(offset=0, hex=_ELF64LE_PREFIX.hex()),       # magic + 64-bit + LE
        MagicPin(offset=0x12, hex=_EM_PPC64_LE16.hex()),     # e_machine == EM_PPC64
    )),
}
```

where `_ELF64LE_PREFIX = b"\x7fELF\x02\x01"` (magic + `EI_CLASS=64-bit` + `EI_DATA=LE`) and
`_EM_PPC64_LE16 = (21).to_bytes(2, "little")` (`EM_PPC64` at the 16-bit little-endian
`e_machine` field, offset `0x12`).

**Machine-strict, symmetric with x86.** The `e_machine` pin is load-bearing: the
ELF64-LE prefix alone is common to *every* 64-bit little-endian ELF (an x86_64, aarch64, or
riscv64 `vmlinux` all begin `\x7fELF\x02\x01`), so without the `e_machine` check a non-ppc64
ELF misplaced into the boot slot under `arch: ppc64le` would pass — the same "misplaced
vmlinux" leak the x86 bzImage magic already forecloses. Pinning `EM_PPC64` makes the
ppc64le gate exactly one machine format, matching the x86 side's strictness (a bzImage is
inherently x86). The generalized boot-member check reads the member bytes up to the largest
pin extent and requires **all** pins for the declared arch to match — the multi-pin
generalization of today's single-pin `_member_is_bzimage`.

- `validate_external_artifacts` gains an `arch: str` parameter, threaded from the persisted
  build profile (§3).
- `_check_kernel_combined_tar` / `_verify_combined_tar_shape` / the boot-member check take
  `arch` and match the member bytes against `BOOT_MEMBER_FORMATS[arch]`. For `x86_64` this
  is byte-identical to today.
- An `arch` not in `BOOT_MEMBER_FORMATS` fails fast `CONFIGURATION_ERROR` (the
  `arch_traits()` rule — never a silent x86 fallback). Unreachable in the normal path (the
  profile-parse gate already rejected it), but the validator does not trust its caller.
- A boot member that does not match the declared arch's format is rejected `BUILD_FAILURE`
  with an arch-naming message (e.g. `kernel combined tar boot/vmlinuz is not a ppc64le ELF
  kernel`). This is the "arch mismatch vs profile" rejection: an x86 bzImage (or any
  non-ppc64 ELF) under `arch: ppc64le`, and an ELF under `arch: x86_64`, both fail.

**Scan cap and the stripped boot member.** The 128 MiB decompressed-scan cap
(`_KERNEL_TAR_SCAN_MAX_BYTES`, the gzip-bomb guard) is arch-neutral and **unchanged**. The
contract's ppc64le boot member is the *stripped, bootable* ELF — exactly what Fedora/RHEL
install as `/boot/vmlinuz-<ver>` (tens of MB), **not** the unstripped build-tree `vmlinux`
(which carries full DWARF and is routinely 500 MB–1 GB+). With the stripped image,
`boot/vmlinuz`-first ordering reaches the first `lib/modules` header inside the cap. The
recipe (§4) therefore strips explicitly. A caller who packages the unstripped `vmlinux`
pushes `lib/modules` past the cap and hits the existing
`kernel combined tar has no lib/modules member within the scan bound` rejection; to keep
that failure legible the validator emits, when the boot member *was* seen but the cap was
reached before any `lib/modules` header, a message that names the oversized-boot-member
cause rather than a bare "no lib/modules member." (The DWARF-carrying unstripped ELF belongs
in the optional `vmlinux` artifact, not the boot member.)

### 3. Threading arch through `runs.complete_build`

`runs.complete_build` reads the `arch` from the Run's persisted `build_profile` and threads
it to the validator. The `CompleteBuildValidation` callable type and
`CompleteBuildFinalizer._validate_complete_build` gain the `arch` argument alongside
`manifest`/`keys`/`declared_build_id`; the finalizer passes it into
`validate_external_artifacts`. `complete_build` itself gains **no** new MCP parameter — arch
is intrinsic to the build and already declared on the profile.

**Read the persisted field, do not re-validate the whole profile at finalize.** The handler
reads `arch` directly from the persisted `build_profile` jsonb (`str(...)`, defaulting to
`x86_64` when the key is absent), **not** by re-running `BuildProfile.parse`. The create-time
parse already gated the arch; re-validating the entire profile at finalize would couple
finalize-ability to unrelated profile-schema evolution and — the case that matters — to the
current `SUPPORTED_ARCHES` set. Reading the field keeps a Run that was valid at create
finalizable even if the arch vocabulary later shifts, and the validator's own fail-fast on an
unknown arch (§2) is the defense-in-depth backstop for a genuinely unrecognized value.
Removing an arch from `SUPPORTED_ARCHES` remains a breaking change requiring in-flight build
Runs to be drained; that is out of scope (the epic only *adds* arches).

### 4. Advertisement + agent-facing surface (same PR as the behavior)

- `EXTERNAL_BUILD_CONTRACTS["kernel"]`: the `boot/vmlinuz` `LayoutMember` advertises the
  boot-member format **per arch** from `BOOT_MEMBER_FORMATS`, so `artifacts.expected_uploads`
  shows both the x86_64 bzImage and the ppc64le ELF expectation. The member `note` and the
  artifact `summary` are de-x86-ed: "the bzImage for x86_64 or the ELF `vmlinux` for
  ppc64le; the arch is declared in the build profile."

  **JSON shape (pinned).** The boot member's single `format` object is **replaced** by a
  per-arch map. Today `layout[0]` (the `boot/vmlinuz` member) serializes as
  `{path, required, note, format: {container, magic:[{offset,hex}]}}`. It becomes
  `{path, required, note, formats_by_arch: {"x86_64": {container, magic:[…]},
  "ppc64le": {container, magic:[…]}}}` — the single `format` key is gone, `formats_by_arch`
  is keyed by every arch in `BOOT_MEMBER_FORMATS`. This is a deliberate agent-facing shape
  change on the `kernel` contract (the arch-awareness *is* the feature; per "replace, don't
  deprecate," no dual `format`+`formats_by_arch` shim). The `lib/modules` member (no format)
  and the top-level `kernel`/`vmlinux`/`initrd`/`effective_config` contract shapes are
  untouched. The map is built from `BOOT_MEMBER_FORMATS`, so the no-drift test (ADR-0234 §5)
  still asserts the advertisement equals what the validator enforces, now per arch.
- `runs.create` wrapper (`build_profile` `Field`): document the new `arch` field, its
  allowed values, and the `x86_64` default.
- `runs.complete_build` wrapper docstring: name the per-arch boot-member expectation (the
  boot member format follows the build profile's `arch`).
- `mcp/resources/_content/external-build-upload.md`: replace the x86-literal boot-member
  rule with a per-arch table row (bzImage `HdrS`@`0x202` for x86_64; ELF64-LE + `EM_PPC64`
  `e_machine`@`0x12` for ppc64le) and add a ppc64le `tar` recipe variant. The recipe must
  produce the **stripped** bootable ELF (what installs to `/boot/vmlinuz`), not the
  unstripped build-tree `vmlinux` — otherwise the oversized boot member pushes `lib/modules`
  past the 128 MiB scan cap (§2). Concretely, strip first, then tar the stripped copy:

  ```bash
  ${CROSS_COMPILE}strip -s vmlinux -o /tmp/vmlinuz     # stripped bootable ELF, tens of MB
  tar -czf kernel.tar.gz \
    --exclude='*/build' --exclude='*/source' \
    --transform='s|^vmlinuz$|boot/vmlinuz|' \
    -C /tmp        vmlinuz \
    -C "$MODROOT"  lib/modules
  ```

  Document that the unstripped DWARF `vmlinux` belongs in the optional `vmlinux` artifact,
  not the boot member. The `vmlinux` and `effective_config` optional-artifact rules are
  otherwise unchanged.

## Acceptance criteria

1. **x86_64 validator behavior unchanged.** A build profile with no `arch` (or
   `arch: x86_64`) validates a valid x86 combined tar byte-identically to today, with the
   same rejection messages for a bad member. Existing `validation` *behavior* tests pass
   unmodified. (The `expected_uploads` advertisement JSON does change shape — see #6 — so
   its snapshot/no-drift test is updated deliberately; "unchanged" scopes to validator
   behavior, not the advertisement.)
2. **ppc64le validates.** A build profile `arch: ppc64le` + a combined tar whose
   `boot/vmlinuz` is a ppc64le ELF64-LE kernel (`EM_PPC64`) validates (with a `lib/modules`
   member); the optional `vmlinux` build-id path works for a ppc64le ELF.
3. **Arch mismatch rejected.** Each is rejected `BUILD_FAILURE` with an arch-specific
   message: (a) an x86 bzImage boot member under `arch: ppc64le`; (b) any ELF boot member
   under `arch: x86_64`; (c) a **non-ppc64 ELF64-LE** boot member (e.g. an x86_64 or aarch64
   `vmlinux`) under `arch: ppc64le` — the `e_machine` check, not merely the ELF prefix.
4. **Unknown arch rejected early.** `runs.create` with `build_profile.arch` ∉
   `SUPPORTED_ARCHES` is rejected `CONFIGURATION_ERROR` at parse, before any upload.
5. **Shape-scan bound holds.** The 128 MiB gzip-bomb cap is unchanged and still bounds the
   ELF payload's decompressed scan; a gzip bomb under `arch: ppc64le` is still stopped. When
   the boot member is seen but the cap is reached before any `lib/modules` header (e.g. an
   unstripped boot ELF), the rejection message names the oversized-boot-member cause, not a
   bare "no lib/modules member."
6. **Discoverable + pinned shape.** `artifacts.expected_uploads`, the
   `runs.create`/`runs.complete_build` wrapper text, and `external-build-upload.md` name the
   per-arch payload expectation; the advertisement is derived from the same
   `BOOT_MEMBER_FORMATS` the validator enforces (no-drift test, per ADR-0234). A test asserts
   the concrete `expected_uploads` `kernel` JSON — the boot member's `formats_by_arch` with
   both `x86_64` and `ppc64le` entries, and no legacy single `format` key.

## Scope / non-goals

- No migration, no schema change — `build_profile` is opaque jsonb; an absent `arch`
  defaults to `x86_64`.
- No cross-check of `build_profile.arch` against a bound System's provisioning arch. The
  build lane is decoupled and usually unbound at build time; kernel-vs-System arch
  consistency is a bind/boot-path concern (sub-issue #7), not the artifact contract.
- No boot-path change — sub-issue #7 proves SLOF direct-kernel-boots the uploaded ppc64le
  ELF bundle. This issue is the *upload contract* only.
- No change to the `initrd`/`effective_config` rules; no `.config` correctness gate (ADR
  unchanged).
- No new arch beyond `SUPPORTED_ARCHES`; big-endian ppc64 stays out of scope (epic non-goal).
