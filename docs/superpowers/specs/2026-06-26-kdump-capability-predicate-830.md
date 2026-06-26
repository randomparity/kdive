# Agent-decidable kdump capability per target kernel (issue #830)

- **Issue:** [#830](https://github.com/randomparity/kdive/issues/830)
- **ADR:** [ADR-0253](../../adr/0253-kdump-capability-predicate.md)
- **Depends on:** [#829 / ADR-0252](../../adr/0252-images-describe.md) (`images.describe` +
  `provenance["package_versions"]`), [#817 / ADR-0251](../../adr/0251-local-multidistro-rootfs-catalog.md)
  (the rootfs catalog and the makedumpfile-vs-kernel root cause).
- **Status:** Accepted design.

## Problem

An agent building a from-source kernel cannot decide, before provisioning, which rootfs will
capture a *complete* filtered kdump vmcore for *its* kernel. The model exposes two contradictory
kdump signals and the data needed to decide is not machine-readable.

### Two contradictory signals

1. **`RootfsCatalogEntry.kdump_capable`** (`src/kdive/images/rootfs_catalog.py`) — a stored bool,
   true iff the image's build-time makedumpfile is `>= 1.7.9` (the first release that filters a
   v7.0-class from-source kernel's vmcore). It is computed against **one** kernel basis, it is
   **never read by any runtime code** (only set and guarded by one test), and it is **never
   surfaced to an agent** (no reference under `src/kdive/mcp`).
2. **The `capabilities` tag list** — `build-fs` hardcodes `debug -> ("agent", "kdump", "drgn")`
   (`src/kdive/images/rootfs_command.py`), stamping `"kdump"` on **every** debug image including
   the ones whose `kdump_capable` is `false` (Debian 12/13, all Rocky/CentOS).

The `"kdump"` tag is not *false* — it truthfully asserts that kdump **tooling** is installed
(`kdump.service` exists; `src/kdive/images/validation.py` `GUEST_CONTRACT_PATHS["kdump"]` checks
exactly that, and `images.upload` requires it). The defect is that an agent reads the tag as "can
produce a complete filtered core for my kernel," a different, kernel-relative claim. So the model
says, for every non-`fedora-44` debug image, both "kdump tooling installed = true" (tag) and
"kdump_capable = false" (stored bit) — two claims at different altitudes that read as a
contradiction because the kernel-relative one is hidden.

### The operands are not machine-readable

The real decision is `image_makedumpfile_version >= min_makedumpfile_required(target_kernel)`, and
both operands live only in prose/tests:

- **per-image makedumpfile version** — in TOML comments and a test-only dict
  (`tests/images/test_rootfs_catalog.py` `_MAKEDUMPFILE_BY_NAME`). `provenance["packages"]` records
  the package *name* `makedumpfile`; #829's `provenance["package_versions"]` records its *version*
  **only where it is a standalone package** (Fedora, Debian). On **EL8/EL9 makedumpfile is bundled
  inside `kexec-tools`** (no standalone package; `src/kdive/images/families/rhel.py`), so
  `package_versions` has **no** makedumpfile entry for Rocky/CentOS 8/9.
- **the kernel → min-makedumpfile rule** ("v7.0 needs `>= 1.7.9`") — prose plus a test constant
  `_V7_THRESHOLD`; no machine-readable mapping.

### Why a stale bit bites a newer kernel

The relationship is **monotonic**: an old makedumpfile never *starts* supporting a newer kernel, so
a `false` row stays safe, but a `true` row can silently rot — v7.1 may require a newer makedumpfile
than v7.0 did. A confident `true` is therefore a **false positive**: nothing prompts the agent to
look further, so it provisions, crashes the guest, and only learns at `vmcore.fetch`
(`kdump_core_incomplete` remediation) — post-facto, after committing to the image.

## Goal

Make kdump capability an **agent-decidable predicate** the agent reads from `images.describe`
before provisioning:

1. Both operands become machine-readable for known kernels: the per-image makedumpfile version is a
   real recorded build fact (all families, EL included), and the kernel → min-makedumpfile rule is
   data.
2. Capability is **computed for a given target kernel**, not stored as one kernel-relative bit, and
   always discloses the kernel basis it was computed against.
3. For a target kernel newer than anything characterized, the surface degrades to an explicit
   **`unverified`** with a pointer to the makedumpfile ChangeLog — never a confident `true`.
4. The `"kdump"` tag keeps its honest meaning (tooling installed); the kernel-relative
   core-completeness decision moves to the computed predicate, so the two no longer read as a
   contradiction.

## Design

### 1. The support matrix and the predicate (`src/kdive/images/kdump_support.py`)

A new pure, dependency-free module — the single home for the kernel ↔ makedumpfile rule and the
capability computation. No I/O, fully unit-testable.

- `MakedumpfileVersion(major, minor, patch)` and `KernelVersion(major, minor)` newtypes, each with a
  lenient `parse(str) -> Self` and a total ordering. makedumpfile reports
  `makedumpfile: version 1.7.9 (...)`; the parser extracts the dotted triple. **Kernel comparison is
  on `(major, minor)` only** — a from-source kernel is named `7.0`, `7.0.5`, `7.1.0-rc2`,
  `7.0.0-00123-gdeadbee+`, etc.; the parser reads the leading `major.minor`, treats a missing minor
  as `0`, and **ignores** any `.patch`/`-rc`/`+localversion`/`-gNNN` suffix, so the same dump-format
  generation maps to one matrix bucket (a 7.0.y stable update is *not* pushed past a 7.0 threshold).
  A string with no leading `major[.minor]` integer pair raises `ValueError` (the caller maps that to
  `configuration_error`); makedumpfile parse failures are handled in §4, not by raising to a reader.
- `SUPPORT_MATRIX`: an **ascending tuple of `(KernelVersion, MakedumpfileVersion)` rows** — the
  characterized rule, each row meaning "a kernel in this row's `major.minor` line (up to the next
  row) needs `>= makedumpfile_min`". v1 has one row: `(7.0, 1.7.9)`. `KNOWN_THROUGH: KernelVersion =
  7.0` is the newest kernel line whose requirement we have verified; `DEFAULT_KERNEL_BASIS =
  KNOWN_THROUGH` is the basis used when the caller names no target kernel. The highest row's
  `makedumpfile_min` is `MAX_CHARACTERIZED_REQUIREMENT` (the requirement at `KNOWN_THROUGH`).
- `required_makedumpfile(kernel) -> MakedumpfileVersion | None`: the `makedumpfile_min` of the
  highest row whose `kernel_min <= kernel` (on `major.minor`), or `None` when the kernel is **below
  every characterized row** (no floor — an un-characterized old kernel has no asserted requirement).
- `KdumpCapability` result (frozen): `status` ∈ `capable | incapable | unverified | not_applicable`,
  `target_kernel`, `makedumpfile_version | None`, `min_makedumpfile_required | None`, and a `note`
  (the ChangeLog pointer for `unverified`).
- `kdump_capability(*, makedumpfile_version, target_kernel, kdump_tooling) -> KdumpCapability`, in
  this order:
  - `kdump_tooling is False` (image has no `"kdump"` tag) → `not_applicable`.
  - `makedumpfile_version` is `None` or unparseable → `unverified`, note: rebuild to capture / odd
    version string (§4).
  - `target_kernel.major_minor > KNOWN_THROUGH` → `unverified`, note: "makedumpfile `<v>` shipped;
    the minimum for kernel `<k>` is unverified — check the makedumpfile ChangeLog",
    `min_makedumpfile_required` omitted (`None`).
  - else `req = required_makedumpfile(target_kernel)`:
    - `req is not None` (target falls in a characterized row) →
      `capable`/`incapable` from `makedumpfile_version >= req`.
    - `req is None` (target is **older than every characterized row**) → a confident answer is
      possible only one way: if `makedumpfile_version >= MAX_CHARACTERIZED_REQUIREMENT` the image
      supports kernels up to `KNOWN_THROUGH >= target`, so → `capable`. Otherwise the minimum for
      this un-characterized older kernel is unknown → `unverified` (note: requirement for kernel
      `<k>` not characterized — check the makedumpfile ChangeLog). This is the safety fix: a low
      makedumpfile against a mid-range kernel is **never** assumed capable from a floor, because
      makedumpfile's kernel support is an *upper* bound, not a lower one.

The ChangeLog pointer is a single module constant (the upstream makedumpfile ChangeLog URL).

### 2. Per-image makedumpfile version captured at build (`provenance["makedumpfile_version"]`)

A family-neutral build-time **makedumpfile-version probe** — because `package_versions` cannot see
the EL8/EL9 bundled case (makedumpfile inside `kexec-tools`). The version resolves in two steps,
authoritative-then-fallback:

1. **Run `makedumpfile --version` during the build's existing guest-command step and capture it to a
   marker file**, then read the marker back with the existing read-only `guestfish`/inspect seam.
   The local plane already runs `virt-customize` (executes guest commands) and the build already
   stages readiness markers under `/usr/lib/kdive/` (`drgn-ready`, `allowlisted-helpers`); this adds
   `makedumpfile --version > /usr/lib/kdive/makedumpfile-version 2>/dev/null || true` to the same
   `--run-command` set, then reads that file. This reuses **already-proven** mechanisms (guest
   `--run-command`; read-only file read) rather than executing a guest ELF against a read-only mount
   — a mechanism nothing in the codebase exercises today. The probe is a
   `MakedumpfileProbeSeam = Callable[[Path], str | None]` injected into the build-tools dataclass
   (mirroring #829's `VersionInspectSeam`), so unit tests inject a fake and need no libguestfs.
2. **Fallback:** when step 1 yields nothing (empty marker / `makedumpfile` not on `PATH`), fall back
   to `package_versions["makedumpfile"]` if `package_versions` captured it (Fedora/Debian, where
   makedumpfile is a standalone package). Only when **both** are empty is the field omitted.

- `provenance["makedumpfile_version"] = "<v>"` is the parsed dotted version (the marker's raw
  `makedumpfile: version X` line is parsed by the shared §1 parser before recording).
- **Degrade, do not fail the build.** The probe is advisory provenance, exactly like #829's version
  capture: probe failure / absent tool / unparseable output → log a WARNING, omit the field, publish
  the image. Only new builds populate it; older rows omit it (`describe` then computes `unverified`).
  No schema change (additive within the schemaless `provenance` jsonb).
- **Live proof is required before merge** (this host runs `live_vm`): build at least one Fedora image
  (standalone makedumpfile) and one EL8/EL9 image (bundled makedumpfile) and confirm
  `provenance["makedumpfile_version"]` is populated and correct in both — the marker path on Fedora,
  exercising the bundled-`kexec-tools` case on EL. Without that proof the feature could silently
  degrade every image to `unverified` while looking healthy.
- Scope: the probe is wired into the **local-libvirt** plane (where the kdump-capture path and the
  `kdump_core_incomplete` failure live, the issue's `provider:local-libvirt`). The remote plane
  already records `package_versions`; it inherits the field via the §1 fallback where makedumpfile is
  a standalone package and otherwise omits it (no remote kdump-capture path depends on this).

### 3. The recipe catalog records the version, not a bit

`rootfs_catalog.toml` is the **build recipe** catalog (what `build-fs --image` builds), distinct
from the `image_catalog` DB rows (what got built). Today every row carries `kdump_capable` and the
true version lives in a comment + the test dict.

- **Remove** `RootfsCatalogEntry.kdump_capable` and the TOML `kdump_capable` field.
- **Add** `makedumpfile_version: str` to `RootfsCatalogEntry` and every TOML row — the curated
  per-release snapshot (promoting the existing comment to structured data), verified against distro
  package indexes as before.
- The synthesized legacy fallback row (`rootfs_build.py` `_resolve_entry`) sets
  `makedumpfile_version = ""` (unknown → the predicate yields `unverified`).
- `tests/images/test_rootfs_catalog.py`: delete `_MAKEDUMPFILE_BY_NAME` / `_V7_THRESHOLD`; the guard
  becomes "each row's curated `makedumpfile_version` parses, and the predicate computed against
  `DEFAULT_KERNEL_BASIS` matches the documented capability" — driven by the shared module, so the
  rule lives in one place.

The recipe-catalog `makedumpfile_version` is the curated *expectation*; `provenance` is the
*verified* value the built image actually carries (what `images.describe` reads).

### 4. `images.describe` surfaces the computed capability

`images.describe` gains an optional `target_kernel: str | None`. `data` gains a `kdump` block:

```json
"kdump": {
  "makedumpfile_version": "1.7.9",
  "target_kernel": "7.1",
  "capability": "unverified",
  "min_makedumpfile_required": null,
  "note": "makedumpfile 1.7.9 shipped; the minimum for kernel 7.1 is unverified — check the makedumpfile ChangeLog: <url>"
}
```

- `makedumpfile_version` is read from `entry.provenance["makedumpfile_version"]` (`""` when absent).
  `data.kdump.makedumpfile_version` echoes the **raw stored string** for transparency. If the stored
  string is present but does not parse (an odd makedumpfile build / future format), the capability
  degrades to `unverified` (note: stored makedumpfile version `<raw>` is unrecognized) — a reader
  never raises on image data; only the caller-supplied `target_kernel` can be a
  `configuration_error`.
- `kdump_tooling` is `"kdump" in entry.capabilities`.
- `target_kernel` is the parsed parameter, or `DEFAULT_KERNEL_BASIS` when omitted; the echoed value
  is always the basis the answer was computed against (criterion: never a bare kernel-independent
  bit).
- A malformed `target_kernel` → `configuration_error` naming the field (the existing
  describe parse-failure shape), before the DB read.
- The `"kdump"` tag in `capabilities` is unchanged (tooling installed); the `kdump` block is the
  kernel-relative answer. They no longer contradict.

CLI parity: `kdivectl images describe <id> [--target-kernel <ver>]` passes the optional arg through.
The generated `docs/guide/reference/images.md` is regenerated.

## Acceptance criteria → where satisfied

- *Per-image makedumpfile version is a real build fact, not a test dict, readable via describe* →
  §2 (`provenance["makedumpfile_version"]`) + §4 (`data.kdump.makedumpfile_version`); test dict
  deleted in §3.
- *Kernel → min-makedumpfile rule is data; capability computed per target kernel* → §1
  (`SUPPORT_MATRIX`, `kdump_capability`).
- *No image advertises a kdump capability it cannot satisfy for the target kernel; bit and tag
  agree or the redundant tag is removed* → §3 (stored bit removed) + §1/§4 (computed predicate is
  the kernel-relative answer; the `"kdump"` tag is re-scoped to tooling, documented).
- *Agent reads version + computed capability + kernel basis before provisioning* → §4.
- *A target kernel newer than the known matrix yields `unverified` with the ChangeLog pointer, not
  `true`* → §1 (`target_kernel > KNOWN_THROUGH`) + §4.

## Residual (honest)

For a kernel newer than `KNOWN_THROUGH`, the minimum makedumpfile is an upstream fact (the
makedumpfile ChangeLog) we cannot precompute at catalog-authoring time. The goal is not to
eliminate that tail but to degrade it: both operands are machine-readable for known kernels, and
unknown kernels surface an explicit `unverified` + the authoritative pointer instead of a stale
`true`. Extending coverage is a one-line `SUPPORT_MATRIX` append plus a `KNOWN_THROUGH` bump when a
new threshold is characterized.

## Non-goals

- Live upstream lookup of the makedumpfile ChangeLog at request time (the residual stays a pointer).
- Changing the `images.upload` / `GUEST_CONTRACT_PATHS` `"kdump"` guest-contract requirement (it
  validates tooling presence, which remains correct).
- A schema/migration change (`provenance` is schemaless jsonb; the catalog field and the MCP/CLI
  surface are additive).
- Reworking the `systems.toml` staged-path profile capability vocabulary
  (`kdive-ready-console/ssh/drgn`); those are resource-profile match tags on a different axis from
  image guest-contract tags and are out of scope.
