# ADR 0286 — Honest image capability metadata: validated vocabulary + computed-signal framework

- **Status:** Accepted
- **Date:** 2026-06-30
- **Deciders:** kdive maintainers

## Context

An agent cannot reliably tell from image metadata whether a rootfs image supports a
feature; it finds out by trying, and sometimes gets a false success (#957). Exactly one
capability is real: the computed, test-guarded kdump predicate (ADR-0252/0253, #829/#830),
which reads the build-recorded `provenance["makedumpfile_version"]` against a target kernel
and is surfaced by `images.describe` as `data.kdump`. Everything else is the free-form
`ImageCatalogEntry.capabilities: list[str]` (`image_catalog.capabilities text[]`, no value
constraint), which has **three divergent vocabularies**:

- build-computed (`images/rootfs_specs.py`): debug → `(agent, kdump, drgn)`, build → `(agent, build)`;
- hand-written inventory examples (`systems.toml.example`, `examples/local-libvirt/README.md`):
  `["kdive-ready-console", "ssh", "drgn"]`, `["cloud-init", "ssh"]`;
- a seed-fixture example (`admin/default_fixtures.py`): `["kdive-ready-console"]`.

The **only** consumer that gates on a tag is `images.describe` reading `"kdump" in
entry.capabilities`; every other reference stores/serializes/round-trips it. Tokens like
`ssh`, `console`, `cloud-init` are consumed by nothing, and the `ssh`/`drgn` tags advertise
liveness that does not hold (`ssh` live-broken, #956; live-drgn is gated on the provider's
`supported_introspection` plus the profile `ssh_credential_ref`, not the image — #762/#697).
An agent that reads those tags as capability signals is misled.

See `docs/superpowers/specs/2026-06-30-issue-957-image-capability-metadata-design.md`
and `../specs/top-level-design.md` "Image catalog".

## Decision

Model image capabilities as two distinct, honest things and stop the vocabulary drift.

- **`capabilities` is a build fact, not a liveness guarantee.** A tag means "this tooling /
  trait is baked into the image", never "this feature works end-to-end". That distinction is
  what resolves the "`drgn`/`ssh` tag misleading" complaint: `drgn` honestly means the drgn
  binary is present; whether *live*-drgn works is a separate computed answer combining the
  image trait with provider and profile operands.

- **One closed, validated vocabulary.** A new `Capability` StrEnum — `agent`, `kdump`,
  `drgn`, `build` — is the single source of truth, exactly the set the build bakes.
  `ImageCatalogEntry.capabilities` becomes `list[Capability]`, so an unknown token is a
  Pydantic `ValidationError` at the domain boundary through which every catalog read and
  write funnels (`model_validate` in the reconcile, serialize, and read-tool paths). No DB
  migration and no DB `CHECK`: domain-boundary validation is sufficient because no write
  reaches `image_catalog` except through the model, and it keeps the vocabulary free to
  evolve without a schema change. The three drifted sources converge on the enum: the seed
  fixture's `kdive-ready-console` becomes `agent`, and `ssh`/`console`/`cloud-init` are
  scrubbed from the inventory examples (the build never emitted them).

- **A thin computed-signal framework generalizing ADR-0253.** A frozen `CapabilitySignal`
  (`name`, `operand_keys`, a pure `render(entry, target_kernel)`), and a
  `REGISTERED_SIGNALS` tuple whose single member today is `kdump` (operand
  `("makedumpfile_version",)`, rendering the existing `kdump_support.kdump_capability`
  block). `images.describe` renders `data.capability_signals` by iterating the registry,
  replacing the bespoke top-level `data.kdump` block (a breaking agent-surface change, made
  pre-first-release, mirroring ADR-0283). The framework is deliberately minimal — its value
  is the enforcement scaffolding below, not code reuse across one implementation.

- **Degrade-to-unverified is the framework invariant.** Every registered signal reading a
  missing or empty operand returns a non-confident status (`unverified`/`not_applicable`),
  never a confident `capable`. This inverts today's failure mode: a hand-typed tag asserts a
  capability that may be false (lies confidently), whereas a computed signal over an absent
  operand says "unverified — rebuild to characterize" (fails honest). It is also the
  answer to "when #952/#954/etc. land, the build must record the operand and the metadata
  must refresh": an image whose metadata predates a newly-registered signal reads
  `unverified`, never a stale confident answer, so old metadata cannot lie.

- **A documented, guarded roadmap.** A `PLANNED_SIGNALS` manifest records the future signals
  the audit named — `sysrq` (#952), `ssh_reachable` (#956), `live_drgn` (#762/#697),
  `direct_kernel_bootable` (#954) — each with its tracking issue and why it is not honestly
  computable yet. The vmcore-fetch use-site gate (below) is recorded the same way. These are
  not emitted; they are the tracked, enforced backlog.

- **Enforcement guards (unit tests).** (1) Every capability token any code path emits
  (`rootfs_specs._KIND_CAPABILITIES`) parses as a `Capability`. (2) Each registered signal
  declares at least one operand key and, rendered against an entry lacking that operand,
  returns a non-confident status (the degrade-to-unverified contract). (3) `PLANNED_SIGNALS`
  names are disjoint from `REGISTERED_SIGNALS` names and never appear as a `Capability` value
  or a rendered signal key. Unit/service tests cover the model and render logic; they cannot
  falsify that a *live* build records an operand end to end (the ADR-0285 stance), so
  degrade-to-unverified is what keeps an un-wired signal safe rather than false.

- **Use-site gating is deferred, not dropped.** Gating `vmcore.fetch` on the kdump capability
  is the issue's third direction and its concrete motivating example (`fedora-kdive-ready-43`
  is `incapable` on a v7.0 kernel yet nothing blocks a `KDUMP` capture). It is deferred to a
  follow-up because the honest gate is method-aware — only the in-guest `KDUMP` method depends
  on `makedumpfile`; `HOST_DUMP`/`GDBSTUB` do not — and needs Run → System →
  `provisioning_profile.rootfs` (catalog name) → `image_catalog` provenance plus the booted
  kernel version, refusing only on a confidently `incapable`/`not_applicable` image and
  failing open on any resolution uncertainty. That is a bounded slice of its own; this ADR
  builds the framework it will consume and registers it as a planned use-site.

## Consequences

- Agents get one validated capability vocabulary and a generalizable
  `data.capability_signals` block whose only honest member today is `kdump`, computed exactly
  as before. The misleading `ssh`/`console`/`cloud-init` example tokens are gone.
- `data.kdump` is renamed to `data.capability_signals["kdump"]`; the `images.describe`
  wrapper docstring, the generated tool reference (`just docs`), and the tests that read the
  block are updated in this PR. Pre-first-release, no compatibility shim.
- Typing `capabilities` as `list[Capability]` makes any legacy/junk token in a seeded row a
  hard `ValidationError` on read rather than silent passthrough; the seeds and fixtures are
  aligned in the same change so no in-tree data trips it.
- Adding a future capability is an enum member plus a build that bakes it; adding a future
  computed signal is a `CapabilitySignal` plus a build that records its operand. Until the
  build records the operand, the signal reads `unverified` everywhere — safe, not false —
  and the guards force the vocabulary and planned/registered sets to stay consistent.
- No new MCP tool, RBAC change, schema/migration, or config change. Tool visibility is
  unchanged, so the RBAC matrix is untouched.

## Alternatives considered

- **Add computed predicates for `sysrq`/`ssh_reachable`/`live_drgn`/`direct_kernel_bootable`
  now.** Rejected: those features are broken or unmodeled and blocked on #952/#954/#955/#956,
  so no honest per-image operand exists yet. Emitting them would recreate the exact
  inaccurate-advisory problem this ADR removes. They are recorded as `PLANNED_SIGNALS`
  instead, computed only once their build operand is real.
- **A DB `CHECK` constraint on `capabilities` (migration).** Rejected in favor of
  domain-boundary validation: every write funnels through the model, so the enum already
  fails closed, and a `CHECK` would make every vocabulary change a schema migration for a
  closed set that evolves at the domain layer.
- **Drop the `drgn` (and `agent`/`build`) tags entirely as "misleading / unconsumed".**
  Rejected: once `capabilities` means "tooling baked in", `drgn` is an honest build fact and
  a future operand for the planned `live_drgn` signal; dropping it would lose a real trait.
  The fix is to define the semantics and validate the vocabulary, not to prune honest facts.
- **Keep `data.kdump` and bolt new signals on beside it.** Rejected: two shapes for one
  concept invites the very drift this ADR removes; one generalized `data.capability_signals`
  is the single evolving surface.
- **A richer plugin/registry (dynamic discovery, per-signal input schemas) for one signal.**
  Rejected as premature abstraction: the requirement is enforced honesty and a tracked
  backlog, met by a tuple, a frozen dataclass, and three guard tests — not a framework.
- **Gate `vmcore.fetch` on the kdump capability in this PR.** Deferred (see Decision): the
  honest gate is method-aware and needs cross-object resolution with fail-open semantics —
  a follow-up slice that consumes this framework rather than part of the model change.
