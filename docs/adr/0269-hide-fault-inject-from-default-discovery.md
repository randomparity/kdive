# ADR-0269: hide the fault-inject fixture from default agent discovery (#879)

- Status: Proposed
- Date: 2026-06-28

## Context

`fault-inject` is a test/mock provider (deterministic crash replay, ADR-0072) — a fixture for
exercising the crash/capture planes without real hardware, not a production provisioning lane.
But `systems.profile_examples` (ADR-0124), the agent-facing "learn a valid profile shape from the
MCP surface" discovery tool, advertises it as a first-class provider variant whenever the inventory
configures *no* provider instance: `_configured_providers` returns `[local-libvirt, remote-libvirt,
fault-inject]` both when `systems.toml` is absent (the gitignored pre-config state) and when a doc
configures no instance. A black-box agent driving the server (`BLACK_BOX_REVIEW.md` P2) read the
fault-inject example in depth and tried to allocate it; the request only failed because no
`fault-inject` resource was registered (`available kinds: local-libvirt`). The capability is *sold*
to the agent, then absent — a wasted agent turn on a dead end.

The tool already emits a fault-inject example **only when `doc.fault_inject` is configured** in the
non-default branch; the defect is purely the two default/fallback paths (no file, or a file
configuring nothing) seeding the full three-provider set. When an operator *does* declare a
`[[fault_inject]]` instance, that is a deliberate test/dev environment and surfacing the example is
correct — that branch is unchanged.

Two adjacent items in the issue's evidence are **not** fault-inject leaks: the `fixtures` namespace
(`tool_index.py` TOC, `mcp/tools/catalog/fixtures.py`) is the **rootfs baseline catalog** (ADR-0089
§6, default profiles all `local-libvirt`), and the `resources.register_fault_inject` tool plus the
`_KIND_BY_BLOCK` map are `platform_admin`-only operator surface, not general agent discovery.

## Decision

1. **Gate the example on configuration, not on a default.** `_configured_providers` drops
   `fault-inject` from both default/fallback lists. The provider set becomes:
   `[local-libvirt, remote-libvirt]` when no instance is configured, plus `fault-inject` **iff**
   `doc.fault_inject` is non-empty. An agent against a server with no `[[fault_inject]]` block never
   sees the fixture advertised; an operator who configures one still gets the example.

2. **Self-mark a surfaced fixture example test-only.** Every `profile_examples` item gains a
   `test_only` boolean in its `data` (uniform key across providers): `False` for
   local-libvirt/remote-libvirt, `True` for fault-inject. When the fixture *does* appear (configured
   environment), the agent reads an explicit machine-readable marker that it is a test fixture, not
   a production lane.

3. **Mark the enum member test-only at the source and in the one agent-facing schema that keeps it.**
   `ResourceKind.FAULT_INJECT` gains a comment documenting it as the ADR-0072 test fixture, distinct
   from the production provider kinds. `allocations.request` still accepts the kind (the provider is
   real in test/dev), so `ResourceByKind.kind` gains a `Field(description=...)` naming `fault-inject`
   as a test/mock fixture — a `StrEnum` field serializes to a bare JSON-schema `enum` with no
   per-member text, so the field description is the only schema-visible place to mark the value
   test-only for an agent inspecting the tool. Runtime `available_kinds` already reports what is
   actually registered.

4. **A guard test pins the default.** `test_systems_profile_examples.py` asserts that
   `build_profile_examples(None)` and a doc with no `fault_inject` emit no `fault-inject` item, that
   a doc with `[[fault_inject]]` does emit one carrying `test_only=True`, and that the production
   examples carry `test_only=False`. A regression that re-adds the fixture to the default set fails
   CI.

No DB migration, RBAC change, config flag, or tool-surface (schema) change: this narrows the *data*
an existing read-only tool emits and adds one boolean field to its open `data` object (ADR-0170).

## Consequences

- A cold agent on a stock server is offered exactly the two production provider shapes it can
  actually provision; the mock crash provider no longer burns a discovery→allocate turn.
- An operator running a fault-inject test environment (declared `[[fault_inject]]`) still gets the
  example, now self-marked `test_only=True`.
- `profile_examples` items carry one new `data` key (`test_only`); the exact-keys guard test and any
  agent reading the envelope must account for it. The output schema (open `data` object) is
  unchanged.
- The default item count drops from 3 to 2; tests that encoded the old "all three by default"
  contract are updated to the new contract in the same change.

## Considered & rejected

- **Gate behind an explicit dev/test env flag (e.g. `KDIVE_ADVERTISE_FAULT_INJECT`).** The issue
  offers this as an alternative, but a flag is a second source of truth for "is this a fault-inject
  environment" — the `[[fault_inject]]` inventory block already *is* that signal, and the tool
  already reads it. A flag would let a server advertise a provider it has not configured, the exact
  mismatch this fixes. Rejected as redundant config surface.
- **Drop fault-inject from the `ResourceKind` enum / `allocations.request` schema entirely.** The
  provider is real and registerable in test/dev; removing the kind would break the fault-inject
  provider and its tests. The enum stays; the kind is marked test-only in the `allocations.request`
  schema (field description) and the *discovery* surface stops defaulting to it.
- **Rename or remove the `fixtures` namespace from the TOC.** It is the rootfs baseline catalog
  (ADR-0089), not the fault-inject provider — no fault-inject string appears there. Renaming a
  correct, unrelated namespace would be out-of-scope churn.
- **Surface a `note` string instead of a `test_only` boolean.** The item already carries a generic
  `note`; an agent must parse prose to learn test-only-ness. A dedicated boolean is the
  machine-readable marker the acceptance criterion asks for.
