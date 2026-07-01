# Design — Honest image capability metadata (#957)

- **Issue:** #957
- **ADR:** [0286](../../adr/0286-image-capability-metadata.md)
- **Date:** 2026-06-30
- **Status:** Accepted

## Problem

An agent cannot tell from image metadata whether a rootfs image supports a feature
(SysRq, SSH, live-drgn, gdbstub, direct-kernel boot). It finds out by trying, and sometimes
gets a false success. Only one capability is honest: the computed, test-guarded kdump
predicate (ADR-0253) surfaced by `images.describe` as `data.kdump`. Everything else lives in
a free-form `ImageCatalogEntry.capabilities: list[str]` (`image_catalog.capabilities text[]`,
no value constraint) that:

- has **three divergent vocabularies** — build-computed `{agent, kdump, drgn, build}`
  (`images/rootfs_specs.py`), hand-written inventory examples `{kdive-ready-console, ssh,
  drgn, cloud-init}` (`systems.toml.example`, `examples/local-libvirt/README.md`), and a
  seed-fixture example `{kdive-ready-console}` (`admin/default_fixtures.py`);
- is consumed by exactly one site (`"kdump" in entry.capabilities`);
- advertises `ssh`/`drgn` liveness that does not hold (`ssh` live-broken #956; live-drgn is
  gated on provider `supported_introspection` + profile `ssh_credential_ref`, not the image).

## Goals

1. One validated capability vocabulary; kill the drift.
2. Generalize the ADR-0253 predicate into a small computed-signal framework, so honest
   feature signals are computed from build-recorded facts, not asserted by hand-typed tags.
3. Make the framework's extensibility **enforced**: when a blocked sibling feature (#952,
   #954, #955, #956) lands, its build must record the operand and stale metadata must never
   report a confident-but-wrong answer.

## Non-goals

- Computing `sysrq`/`ssh_reachable`/`live_drgn`/`direct_kernel_bootable` now — those features
  are broken or unmodeled and blocked on sibling issues; no honest per-image operand exists
  yet. They are recorded as a guarded backlog.
- Gating `vmcore.fetch` on the kdump capability — deferred to a follow-up (method-aware,
  cross-object resolution; see below).
- Any DB schema/migration, new MCP tool, RBAC, or config change.

## Design

### 1. Reframe `capabilities` as a build fact

A `capabilities` tag means "this tooling / trait is baked into the image", **never** "this
feature works end-to-end". "Does feature X work for target Y" is answered separately by a
computed signal. This resolves the "`drgn` tag misleading" complaint without pruning a real
fact: `drgn` honestly means the drgn binary is present and is a future operand for the
planned `live_drgn` signal.

### 2. Closed, validated vocabulary — `Capability`

New `Capability(StrEnum)` in `domain/catalog/images.py`:

```
agent    — the kdive guest agent / console readiness is baked in
kdump    — kdump tooling (kexec-tools, makedumpfile) is baked in
drgn     — the drgn binary is baked in
build    — the kernel build toolchain is baked in
helpers  — the allowlisted in-guest helpers are baked in
```

The build bakes `agent`/`kdump`/`drgn`/`build` (`rootfs_specs._KIND_CAPABILITIES`); `helpers`
is the fifth member because the private-upload path lets an operator declare it. That upload
path validates a declared `required` set against `images/validation.py:GUEST_CONTRACT_PATHS`
(a fourth vocabulary the audit missed — `agent`/`kdump`/`drgn`/`helpers`) and then stores it
as the image's `capabilities`; a guard test keeps `GUEST_CONTRACT_PATHS` keys a subset of
`Capability` (`build` alone has no in-guest marker), so the upload boundary
(`validate_guest_contract`, which runs before the DB write) enforces the unified vocabulary
and a valid guest-contract element never fails the capability read-back.

`capabilities` is carried by **five** models, not one, and the read-tool model
(`ImageCatalogEntry`) is *not* on the write path — so typing only it would leave the primary
ingestion path (inventory TOML → reconcile → `image_catalog`) unvalidated. All five are
typed `list[Capability]` so the vocabulary is enforced wherever a token is authored, read,
or serialized:

| Model | File | Role | Boundary |
|---|---|---|---|
| `ImageEntry` | `inventory/model.py` | authored inventory TOML → reconcile **insert/update** | **write** |
| `RootfsCatalogEntry`, `RootfsRequirements` | `components/catalog.py` | authored fixture-manifest rows | **write** |
| `ImageCatalogEntry` | `domain/catalog/images.py` | DB row → `images.list`/`describe` | read |
| `ImageRow` | `inventory/serialize.py` | DB row → serialized inventory TOML | read/out |
| `RootfsBuildSpec` | `images/planes/base.py` | internal build spec (from `_KIND_CAPABILITIES`) | internal |

Enforcement is Pydantic enum coercion at each *Pydantic* model — an unknown token is a
`ValidationError` when inventory TOML is loaded (`ImageEntry`), when a fixture manifest is
parsed (`RootfsCatalogEntry`/`RootfsRequirements`), and when a DB row is read by a read tool
(`ImageCatalogEntry`). `ImageRow` and `RootfsBuildSpec` are frozen `dataclass`es, not
Pydantic models, so a typed field alone does not validate at runtime: `ImageRow`'s
`serialize.py` constructor coerces each token via `Capability(cap)` (raising on an
off-vocabulary DB token — the serialize-out enforcement, replacing the raw
`[str(cap) for cap in capabilities]`), and `RootfsBuildSpec.capabilities` becomes
`tuple[Capability, ...]` sourced directly from the enum-typed `_KIND_CAPABILITIES`, so it
cannot carry an off-vocabulary token by construction. No DB
`CHECK`, no migration: every write reaches `image_catalog` through one of the write-boundary
models, and the closed set evolves at the domain layer without a schema change.

Converge the drifted sources onto the enum:
- `admin/default_fixtures.py`: `kdive-ready-console` → `agent`.
- `systems.toml.example`, `examples/local-libvirt/README.md`: scrub `ssh`/`console`/
  `cloud-init`; use real enum values (`agent`, `kdump`, `drgn`, `build`).
- `rootfs_specs._KIND_CAPABILITIES`: emit `Capability` members.

### 3. Computed-signal framework

New `images/capability_signals.py`:

```
@dataclass(frozen=True, slots=True)
class CapabilitySignal:
    name: str                       # "kdump"
    operand_keys: tuple[str, ...]   # provenance keys the build must record
    render: Callable[[ImageCatalogEntry, KernelVersion], dict[str, JsonValue]]

KDUMP_SIGNAL = CapabilitySignal(
    name="kdump",
    operand_keys=("makedumpfile_version",),
    render=render_kdump_signal,     # wraps images.kdump_support.kdump_capability
)
REGISTERED_SIGNALS: tuple[CapabilitySignal, ...] = (KDUMP_SIGNAL,)
```

`images.describe` renders `data.capability_signals = {sig.name: sig.render(entry, basis)
for sig in REGISTERED_SIGNALS}`, replacing the bespoke top-level `data.kdump` block
(breaking agent-surface change, pre-first-release, per ADR-0283). The `kdump` block content
is byte-for-byte what `data.kdump` returned today.

**Degrade-to-unverified invariant.** Every registered signal reading a missing/empty operand
returns a non-confident status (`unverified` / `not_applicable`), never a confident
`capable`. `kdump_capability` already does this (absent `makedumpfile_version` →
`unverified`). This is the guarantee that an image whose metadata predates a newly-registered
signal reads `unverified`, not a stale confident answer — so old metadata cannot lie, and a
signal registered before its build wiring lands is safe (honest) rather than false.

### 4. Guarded roadmap — `PLANNED_SIGNALS`

```
@dataclass(frozen=True, slots=True)
class PlannedSignal:
    name: str
    tracking_issue: str
    rationale: str

PLANNED_SIGNALS: tuple[PlannedSignal, ...] = (
    PlannedSignal("sysrq", "#952", "SysRq can report false success; needs a build-recorded operand"),
    PlannedSignal("ssh_reachable", "#956", "sshd/keygen liveness is broken; not an honest per-image fact yet"),
    PlannedSignal("live_drgn", "#762/#697", "drgn liveness depends on provider introspection + profile ssh_credential_ref"),
    PlannedSignal("direct_kernel_bootable", "#954", "direct-kernel provisionability is discovered only by failure"),
)
```

The deferred `vmcore.fetch` kdump gate is recorded in the ADR as a planned use-site. These
are documentation-with-teeth: not emitted, and guarded to stay disjoint from the registered
set.

### 5. Enforcement guards (unit tests)

- **Vocabulary:** every token `rootfs_specs._KIND_CAPABILITIES` emits parses as a
  `Capability`. (The examples/fixtures are aligned in the same change.)
- **Degrade-to-unverified:** each registered signal declares ≥1 operand key and, rendered
  against an entry whose provenance lacks that operand, returns a status not in
  `{capable}` — the honest-stale contract.
- **Planned vs registered:** `{p.name for p in PLANNED_SIGNALS}` is disjoint from
  `{s.name for s in REGISTERED_SIGNALS}`, and no planned name is a `Capability` value or a
  rendered signal key.

Unit/service tests cover the model and render logic; they cannot falsify that a *live* build
records an operand end to end (the ADR-0285 stance). Degrade-to-unverified is what keeps an
un-wired signal safe rather than false.

## Deferred: `vmcore.fetch` use-site gate

The issue's third direction and concrete motivating example (`fedora-kdive-ready-43` is
`incapable` on a v7.0 kernel yet nothing blocks a `KDUMP` capture). Deferred because the
honest gate is **method-aware** — only the in-guest `KDUMP` `CaptureMethod` depends on
`makedumpfile`; `HOST_DUMP`/`GDBSTUB` do not — and needs Run → System →
`provisioning_profile.rootfs` (catalog name) → `image_catalog` provenance plus the booted
kernel version, refusing only on a confidently `incapable`/`not_applicable` image and
failing open on any resolution uncertainty. This ADR builds the framework it will consume; a
follow-up issue implements the gate.

## Acceptance criteria

- All five `capabilities` carriers (`ImageEntry`, `RootfsCatalogEntry`/`RootfsRequirements`,
  `ImageCatalogEntry`, `ImageRow`, `RootfsBuildSpec`) are `Capability`-typed; an unknown
  token fails validation on inventory-TOML load, fixture-manifest parse, read-tool read, and
  serialize-out — verified by a test that feeds each write-boundary model a junk token.
- The three vocabularies are converged; no example emits `ssh`/`console`/`cloud-init`.
- `images.describe` returns `data.capability_signals` (with a `kdump` member equal to the old
  `data.kdump`); the wrapper docstring and generated tool reference are updated.
- `REGISTERED_SIGNALS` has one honest member (`kdump`); `PLANNED_SIGNALS` records the four
  future signals with tracking issues.
- The three guard tests pass; a missing operand yields a non-confident status.
- A follow-up issue tracks the method-aware `vmcore.fetch` kdump gate.
- `just ci` is green (lint, type, docs-check, adr-status-check, tests).
