# ADR-0253: kdump capability as a computed per-kernel predicate (#830)

- Status: Accepted
- Date: 2026-06-26

## Context

A from-source kernel's vmcore can only be filtered to a complete kdump core by a makedumpfile
new enough for that kernel (ADR-0251 / #817: a v7.0-class kernel needs makedumpfile `>= 1.7.9`).
An agent choosing a rootfs cannot decide this before provisioning, and the model carries two
signals that read as a contradiction.

### Two contradictory signals

1. `RootfsCatalogEntry.kdump_capable` (`src/kdive/images/rootfs_catalog.py`) — a stored bool,
   `true` iff the image's build-time makedumpfile is `>= 1.7.9`. It is computed against a single
   kernel basis, **never read by any runtime code** (set + one test guard only), and **never
   surfaced to an agent** (no reference under `src/kdive/mcp`).
2. The `capabilities` tag list — `build-fs` hardcodes `debug -> ("agent", "kdump", "drgn")`
   (`src/kdive/images/rootfs_command.py`), stamping `"kdump"` on **every** debug image, including
   ones whose `kdump_capable` is `false` (Debian 12/13, all Rocky/CentOS).

The `"kdump"` tag is truthful at its altitude: it asserts kdump **tooling** is installed
(`kdump.service` exists; `GUEST_CONTRACT_PATHS["kdump"]` checks exactly that and `images.upload`
requires it). The defect is that an agent reads it as "produces a complete filtered core for my
kernel" — a kernel-relative claim the tag does not make. The kernel-relative answer (`kdump_capable`)
is hidden, so the two surface as a contradiction.

### The operands are not machine-readable

The decision is `image_makedumpfile_version >= min_makedumpfile_required(target_kernel)`; both
operands live only in prose/tests:

- **per-image makedumpfile version** — TOML comments + a test-only dict
  (`tests/images/test_rootfs_catalog.py` `_MAKEDUMPFILE_BY_NAME`). ADR-0252's
  `provenance["package_versions"]` captures it only where makedumpfile is a **standalone package**
  (Fedora, Debian). On **EL8/EL9 makedumpfile is bundled in `kexec-tools`**
  (`src/kdive/images/families/rhel.py`), so `package_versions` has no makedumpfile entry for
  Rocky/CentOS 8/9.
- **the kernel → min-makedumpfile rule** — prose plus the test constant `_V7_THRESHOLD`; no
  machine-readable mapping.

### Monotonicity makes a stale bit dangerous

An old makedumpfile never *starts* supporting a newer kernel, so a `false` row stays safe while a
`true` row can silently rot (v7.1 may need a newer makedumpfile than v7.0). A confident `true` is a
false positive: nothing prompts a second look, so the agent provisions, crashes the guest, and only
learns at `vmcore.fetch` (`kdump_core_incomplete`) — after committing to the image.

## Decision

Replace the stored `kdump_capable` bit with a **computed per-kernel predicate** an agent reads from
`images.describe` before provisioning. Four parts.

1. **A pure support-matrix + predicate module** (`src/kdive/images/kdump_support.py`).
   `MakedumpfileVersion` / `KernelVersion` newtypes with parsers and total ordering; an ascending
   `SUPPORT_MATRIX` of `(KernelVersion, MakedumpfileVersion)` rows (v1: one row `(7.0, 1.7.9)`) with
   `KNOWN_THROUGH = 7.0` and `DEFAULT_KERNEL_BASIS = KNOWN_THROUGH`; `min_makedumpfile_required(kernel)`
   (highest row with `kernel_min <= kernel`, floor `0.0.0` for kernels predating every row); and
   `kdump_capability(*, makedumpfile_version, target_kernel, kdump_tooling) -> KdumpCapability` with
   `status` ∈ `capable | incapable | unverified | not_applicable`. `not_applicable` when the image
   has no `"kdump"` tag; `unverified` when the version is unknown or `target_kernel > KNOWN_THROUGH`
   (note + makedumpfile ChangeLog pointer); otherwise the `>=` comparison. No I/O.

2. **Capture the makedumpfile version at build** into `provenance["makedumpfile_version"]` via a
   dedicated, family-neutral binary probe (`makedumpfile --version`, read-only) injected into each
   build plane's tools dataclass as a `MakedumpfileProbeSeam` (mirroring ADR-0252's
   `VersionInspectSeam` so unit tests inject a fake). A binary probe — not `package_versions` —
   because only the binary is visible on EL8/EL9. **Degrade, do not fail the build**: probe failure
   / absent tool logs a WARNING and omits the field (only new builds populate it; older rows omit
   it). Additive within the schemaless `provenance` jsonb — no schema/migration.

3. **The recipe catalog records the version, not a bit.** Remove `RootfsCatalogEntry.kdump_capable`
   and the TOML `kdump_capable` field; add `makedumpfile_version: str` to the row and every
   `rootfs_catalog.toml` entry (promoting the existing comment to structured data). The synthesized
   legacy fallback row sets `makedumpfile_version = ""` (→ `unverified`). The catalog test guard
   becomes "each row's curated version parses and the predicate against `DEFAULT_KERNEL_BASIS`
   matches the documented capability," driven by the shared module (`_MAKEDUMPFILE_BY_NAME` /
   `_V7_THRESHOLD` deleted).

4. **`images.describe` surfaces the predicate.** Add optional `target_kernel: str | None`; add a
   `data.kdump` block (`makedumpfile_version`, echoed `target_kernel` basis, `capability`,
   `min_makedumpfile_required`, `note`) computed from `provenance["makedumpfile_version"]`,
   `"kdump" in capabilities`, and the parsed target kernel (or `DEFAULT_KERNEL_BASIS`). A malformed
   `target_kernel` is a `configuration_error` before the DB read. The `"kdump"` tag is unchanged
   (tooling); the block is the kernel-relative answer. CLI gains
   `kdivectl images describe <id> [--target-kernel <ver>]`; `docs/guide/reference/images.md` is
   regenerated.

## Consequences

- An agent reads makedumpfile version + computed capability + the disclosed kernel basis from
  `images.describe` before provisioning, instead of discovering incompleteness at `vmcore.fetch`.
- The `"kdump"` tag and the capability no longer contradict: the tag is tooling presence, the
  `data.kdump` block is kernel-relative core-completeness.
- The kernel→makedumpfile rule lives in exactly one place (the shared module); extending coverage is
  a `SUPPORT_MATRIX` append plus a `KNOWN_THROUGH` bump.
- `provenance["makedumpfile_version"]` is populated only for images built after this change; describe
  computes `unverified` for older rows. An operator who wants it rebuilds the image.
- Both build planes gain a read-only `makedumpfile --version` probe on the build host; it degrades
  to omitted on a host without it, so the dependency is soft (as ADR-0252's `virt-inspector`).
- `kdump_capable` is removed from the catalog row and TOML — a breaking change to the recipe
  catalog's shape, but it has no runtime reader and the TOML is operator-edited in-repo.

## Considered & rejected

- **Keep `kdump_capable` as a stored bit and just surface it.** It is kernel-relative; surfacing a
  bare bit reproduces the false positive the issue is about (a `true` rots for a newer kernel).
- **Compute capability only against a fixed default kernel** (no `target_kernel` param). The use
  case is an agent deciding for *its* from-source kernel; a fixed basis forces the agent to
  re-derive the rule it cannot see. The optional param defaults to the characterized basis, so the
  common path is unchanged while the real question is answerable.
- **Reuse `package_versions["makedumpfile"]` instead of a binary probe.** Misses EL8/EL9 (bundled in
  `kexec-tools`), leaving every Rocky/CentOS 8/9 image permanently `unverified` — the binary probe
  is authoritative across all families.
- **Make version capture a hard build gate.** Regresses build reliability for advisory metadata; a
  transient probe failure would fail an otherwise-good image. Degrade-don't-fail instead
  (consistent with ADR-0252 and ADR-0194).
- **Drop the `"kdump"` capability tag entirely.** Would force churn through `DEFAULT_REQUIRED_CONTRACT`
  and `GUEST_CONTRACT_PATHS` (which correctly validate tooling presence) for no gain; the tag's
  honest meaning is retained and the kernel-relative claim is moved to the computed block.
- **Live makedumpfile-ChangeLog lookup at request time.** The residual (requirement for an
  un-characterized kernel) stays a pointer, not a network fetch on a read path.
- **Reshape `provenance` into a versioned schema / add a DB column.** The schemaless jsonb absorbs
  the additive field; no migration is warranted for one advisory value.
