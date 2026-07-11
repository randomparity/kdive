# ADR 0175 — Explain partial tool maturity via a structured `maturity_detail`

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-18
- **Deciders:** David Christensen
- **Spec:** [`../superpowers/specs/2026-06-18-partial-tool-maturity-reason.md`](../archive/superpowers/specs/2026-06-18-partial-tool-maturity-reason.md)
- **Builds on:** [ADR-0047](0047-agent-facing-tool-guide-generation.md) (the registry-derived tool
  reference and the `maturity` marker this ADR explains).

## Context

ADR-0047 gives every MCP tool a `maturity` marker (`implemented` / `partial` /
`planned`) in its `meta` dict, rendered as a badge in the generated reference and
checked for validity by `tests/mcp/core/test_tool_docs.py`. 26 tools are `partial`.

A black-box agent sees `partial` but not the reason. The marker conflates distinct
situations: a tool fully wired but exercised only under the gated `live_vm`/
`live_stack` markers, a tool whose provider seam is a stub, a tool behind an operator
gate, a worker path not yet proven end-to-end. Without the reason the agent cannot
plan: it cannot tell a tool it should trust on a live host from one it should avoid,
and it has no documented bar for when `partial` becomes `implemented`.

## Decision

Add a structured `maturity_detail` object to the `meta` dict of every `partial` tool,
read off the same registry channel as `maturity`:

- A closed `MaturityReason` enum (`provider_support`, `live_dependency`,
  `unproven_worker_path`, `operator_gate`, `degraded_stub`) so the category is
  machine-comparable, not free text.
- A one-line `detail` (why it is partial today) and a one-line `promotion` (the bar to
  reach `implemented`) — both required for `partial`.
- An optional one-line `providers` pointer for provider-dependent tools, naming which
  of local-libvirt / remote-libvirt / fault-inject the path is wired for.

A single constructor `maturity_meta(...)` in `mcp/tools/_docmeta.py` builds the `meta`
dict and enforces the invariants at registration: `partial` requires
reason/detail/promotion; `implemented`/`planned` reject them. The generator
(`scripts/gen_tool_reference.py`) renders a **Maturity / Promotion / Provider
support** block under the badge for `partial` tools, and `test_tool_docs.py` fails
when a `partial` tool lacks the reason or a non-partial tool carries a stale one.

Enforcement is three-layered: the constructor raises at registration (earliest), the
generator's `tool_docs` raises (gates `just docs-check`), and the tests assert the
registry (gates `just test`).

## Consequences

- Agents read a categorized reason and a provider note off `meta`, the same surface as
  `maturity`. The generated reference shows it to humans.
- Promotion `partial` → `implemented` now has a written, per-tool bar (`promotion`),
  reviewable in the diff that flips the marker.
- Adding a new `partial` tool without a reason fails three independent guards.
- The provider note is a short pointer, not a per-provider × per-plane matrix; the
  authoritative provider-support state stays in the provider compositions
  (`supported_capture_methods`, the implemented port protocols). This trades a small
  drift risk for not duplicating a second source of truth.
- No runtime, envelope, schema, or DB change; reversible by revert.

## Considered & rejected

- **A free-text reason string only.** Simpler, but the *category* (provider vs. live
  vs. gate vs. stub) is the part an agent branches on; an enum keeps it comparable and
  the vocabulary closed. Kept the free-text `detail` *alongside* the enum for the human
  specifics.
- **A full per-provider × per-plane support matrix in tool metadata.** Authoritative
  but duplicates state already in the provider compositions and drifts the moment a
  provider gains a plane. Rejected in favor of a short `providers` pointer plus the
  existing compositions as source of truth.
- **A new top-level `meta` key per field (`maturity_reason`, `maturity_promotion`,
  …).** Flatter, but spreads one concern across several keys with no structural link;
  nesting under `maturity_detail` keeps "all the partial explanation" in one object the
  constructor can validate as a unit.
- **A separate DB table / migration for tool maturity.** Over-engineered: tool
  maturity is source-level metadata that ships with the code, not operator state.
