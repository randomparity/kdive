# ADR 0343 — Arch-aware kernel-artifact upload contract (bzImage vs ELF payload)

- **Status:** Accepted
- **Date:** 2026-07-13
- **Issue:** #1145
- **Epic:** #1139 (full ppc64le support)
- **Builds on:** ADR-0234 (external-build upload contract), `domain/platform/arch_traits.py`
  (`SUPPORTED_ARCHES`), ADR-0339 (arch as a first-class provisioning fact)

## Context

`build_artifacts/validation.py` is x86-literal. The combined `kernel` upload is a gzip
tar whose `boot/vmlinuz` member must carry the x86 **bzImage** `HdrS` magic at offset
`0x202` — the code and the advertised contract both say "the bzImage, NOT the vmlinux
ELF." powerpc has no bzImage: the bootable ppc64le kernel is an **ELF** (`vmlinux`), which
is also what Fedora/RHEL install as `/boot/vmlinuz-<ver>` on ppc64le, and which SLOF boots
via `-kernel`. A valid ppc64le kernel upload is therefore rejected today with
`kernel combined tar has no boot/vmlinuz bzImage member`.

The bzImage-vs-ELF distinction cannot be inferred safely from the payload alone. The
original x86 rule is deliberately strict — "bzImage, **not** the vmlinux ELF" — so a
validator that accepted "bzImage OR any ELF" would silently pass an x86 `vmlinux` uploaded
by mistake into the boot slot. The validator must be told which arch it is checking so it
can enforce one strict format per arch.

The build lane is **decoupled** from a System (ADR-0169): `runs.create` records a Run from a
`build_profile` with an optional `system_id`, and `runs.complete_build` can finalize an
**unbound** Run. So there is no reliable provisioning-profile arch at validation time — the
arch must be carried by a document present on every build Run.

## Decision

Make the kernel-artifact contract arch-aware, keyed off an explicit arch declared on the
build profile.

**Arch declaration lives on `BuildProfile`.** `BuildProfile` gains
`arch: str = "x86_64"`, validated at parse to be a member of
`arch_traits.SUPPORTED_ARCHES` (`{x86_64, ppc64le}`). It is optional and defaults to
`x86_64`, so every existing build profile (`{"schema_version": 1}`) stays valid and x86
behavior is byte-identical. `schema_version` stays `1` — an additive optional field with a
default is a backward-compatible v1 document. An unknown arch is rejected at `runs.create`
(profile parse), **before** any upload round-trip, mapped to `CONFIGURATION_ERROR` by the
existing `BuildProfile.parse` boundary. `SUPPORTED_ARCHES` remains the single source of
truth for the allowed set — adding an arch is one `arch_traits` row, not an edit here.

**Payload check is arch-keyed.** `validate_external_artifacts` gains an `arch: str`
parameter, threaded from `run.build_profile.arch` by `runs.complete_build`. The
`boot/vmlinuz` member magic is resolved from a single per-arch table
(`BOOT_MEMBER_FORMATS: Mapping[str, FormatContract]`) owned by `validation.py`:

- `x86_64` → bzImage `HdrS` magic at offset `0x202` (unchanged).
- `ppc64le` → ELF64-LE prefix `\x7fELF\x02\x01` at offset `0` **and** `e_machine == EM_PPC64`
  (`21`) at the 16-bit little-endian field at offset `0x12` — two `MagicPin`s, both required.

The `e_machine` pin is load-bearing. The ELF64-LE prefix alone matches *every* 64-bit LE ELF
(an x86_64, aarch64, or riscv64 `vmlinux` all begin `\x7fELF\x02\x01`), so without it a
non-ppc64 ELF misplaced into the boot slot under `arch: ppc64le` would pass — the same
"misplaced vmlinux" leak the x86 bzImage magic forecloses. Pinning `EM_PPC64` makes the
ppc64le gate exactly one machine format, symmetric with the x86 side (a bzImage is inherently
x86). The boot-member check requires **all** pins for the declared arch (the multi-pin
generalization of today's single-pin `_member_is_bzimage`).

A boot member that does not match the declared arch's format is rejected `BUILD_FAILURE`
with an arch-naming message (e.g. `kernel combined tar boot/vmlinuz is not a ppc64le ELF
kernel`). This is the "arch mismatch vs profile" rejection: an x86 bzImage (or any non-ppc64
ELF) under a `ppc64le` profile, or an ELF under an `x86_64` profile, fails the gate for its
declared arch. An unknown arch reaching the validator fails fast `CONFIGURATION_ERROR` (the
`arch_traits()` rule — never a silent x86 fallback), though the profile-parse gate makes this
unreachable in the normal path.

**The optional `vmlinux` debug ELF and the shape-scan bounds are already arch-neutral.**
`extract_build_id_ranged` requires ELF64-**LE** (magic, class 2, data 1) — ppc64le
`vmlinux` is 64-bit little-endian, so it already fits; only the tests need an arch
dimension. The 128 MiB decompressed-scan cap (`_KERNEL_TAR_SCAN_MAX_BYTES`) is a
gzip-bomb guard on decompressed *output* and is arch-neutral; it is unchanged. The
contract's ppc64le boot member is the *stripped* bootable ELF Fedora/RHEL install as
`/boot/vmlinuz` (tens of MB), **not** the unstripped build-tree `vmlinux` (full DWARF,
500 MB–1 GB+); the documented recipe strips explicitly, and the unstripped ELF belongs in
the optional `vmlinux` artifact. So `boot/vmlinuz`-first ordering still reaches the first
`lib/modules` header inside the cap; an unstripped boot member hits the existing scan-bound
rejection, which is reworded to name the oversized-boot-member cause.

**The advertisement cannot drift from the validator.** `BOOT_MEMBER_FORMATS` is the single
source; `EXTERNAL_BUILD_CONTRACTS["kernel"]` advertises the boot member's format **per
arch** from that same table, so `artifacts.expected_uploads` shows both the x86_64 bzImage
and the ppc64le ELF expectation. This changes the advertised JSON: the boot member's single
`format` object is **replaced** by `formats_by_arch: {arch: {container, magic:[…]}}` (no dual
`format`+`formats_by_arch` shim — per "replace, don't deprecate"), so its snapshot/no-drift
test is updated deliberately. The "x86_64 unchanged" claim scopes to validator *behavior*,
not the advertisement shape. The agent-facing surface updates in this PR: the `runs.create`
build-profile `Field` (the new `arch`), the `runs.complete_build` wrapper docstring (the
per-arch boot-member expectation), and the `external-build-upload.md` resource (a per-arch
boot-member table plus a stripped-ELF ppc64le `tar` recipe).

## Consequences

- A ppc64le kernel tar with an ELF `boot/vmlinuz` validates; an x86_64 upload is unchanged
  (default arch, same bzImage gate, same messages).
- The declared arch and the payload format are cross-checked: a bzImage (or any non-ppc64
  ELF, via the `e_machine` pin) under `ppc64le`, or an ELF under `x86_64`, is rejected
  `BUILD_FAILURE` naming the arch.
- The `expected_uploads` advertised JSON for the `kernel` boot member changes shape (single
  `format` → `formats_by_arch`); its snapshot/no-drift test is updated. Agents that read the
  boot member's format move from `layout[…].format` to `layout[…].formats_by_arch[arch]`.
- An unknown/unsupported arch is rejected at `runs.create`, before any upload — cheaper
  than a post-upload rejection.
- `runs.complete_build` on an unbound Run works unchanged: the arch comes from the build
  profile, never from a (possibly absent) System.
- The advertised upload contract (`expected_uploads`) and the docs name both arches, so a
  black-box agent learns the ppc64le shape from MCP alone, without a rejection.
- No migration: `build_profile` is opaque jsonb; a persisted profile without `arch` reads
  back as `x86_64` via the model default.

## Rejected alternatives

- **Declare the arch as a `runs.complete_build` parameter instead of on the build
  profile.** Rejected: arch is intrinsic to what is being built and is known at
  `runs.create`. Putting it on the profile lets the whole upload loop
  (`expected_uploads` → `create_run_upload` → `complete_build`) key off one persisted
  fact, and rejects an unknown arch before any upload rather than at finalize.
- **Make `arch` required on `BuildProfile`.** Rejected: it breaks every existing x86 build
  profile and contradicts the acceptance criterion that x86_64 behavior is unchanged. A
  documented default of `x86_64` (validated, not a silent host-derived fallback) preserves
  back-compat while making ppc64le an explicit opt-in.
- **Cross-check `build_profile.arch` against a bound System's provisioning arch at
  `complete_build`.** Rejected for this issue: the build lane is decoupled and typically
  unbound at build time, so the check would fire only for the classic bound-at-create path.
  Arch consistency between a built kernel and the System it boots on is a bind/boot-path
  concern (sub-issue #7), not the artifact contract. Deferred, not dismissed.
- **Infer arch from the payload (accept "bzImage OR ELF").** Rejected: it defeats the
  strict "bzImage, not vmlinux ELF" x86 rule — an x86 `vmlinux` misplaced into the boot
  slot would pass. An explicit declaration keeps one strict format per arch.
- **A `Literal["x86_64", "ppc64le"]` on the profile.** Rejected: it duplicates
  `SUPPORTED_ARCHES` and drifts when a third arch is added. Validating against
  `SUPPORTED_ARCHES` keeps `arch_traits` the single source; the allowed values are named in
  the `Field` description and docs for discoverability.
- **Raise the 128 MiB scan cap for ELF payloads.** Rejected: unnecessary. The bootable
  `vmlinuz` is stripped and small, and `boot/vmlinuz`-first ordering reaches `lib/modules`
  well inside the existing cap; the gzip-bomb guard stays as-is.

## Rollout

Additive and backward compatible. No migration — `build_profile` is opaque jsonb and an
absent `arch` defaults to `x86_64`. x86_64 uploads validate byte-identically to today;
ppc64le is a new, explicitly declared path.
