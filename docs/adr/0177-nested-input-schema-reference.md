# ADR 0177 — Render nested MCP input schemas in the generated tool reference

- **Status:** Accepted
- **Date:** 2026-06-18
- **Deciders:** KDIVE maintainers

## Context

The per-namespace tool reference (`docs/guide/reference/*.md`) is generated from the
live FastMCP registry by `scripts/gen_tool_reference.py` (ADR-0047). For each tool it
emits a parameter table whose `Type` column is just `str(spec.get("type", "any"))`.

Several high-value parameters carry a Pydantic-derived JSON Schema with real structure
that this collapse throws away:

- `runs.create.build_profile` is a top-level `anyOf` of two source lanes
  (`source='server'` / `source='external'`), each an object with its own fields, nested
  `oneOf` component references, and `enum`/`const` discriminators. It renders as `any`.
- `systems.define.profile` / `systems.provision.profile` are objects with a
  provider-keyed section, a `oneOf` rootfs discriminated union, and `enum` boot methods.
  They render as `object`.
- `systems.list.state` advertises the `SystemState` enum; `shape`/`pcie`/`allocation_id`
  render as `any`.

An agent reading the reference cannot see field names, which fields are required, the
valid enum values, or which union variant to pick. ADR-0047's generator "fails loudly on
incomplete metadata"; rendering a structured payload as `any` is exactly the kind of
silent information loss it was meant to prevent, but the type rendering never enforced it.

These schemas are fully inlined (Pydantic emits `$defs`-free, self-contained subschemas
in this codebase's tool parameters), so a recursive walk terminates on data, not on a
reference graph that could cycle.

## Decision

1. **Render structured parameters as nested detail, not a single coarse type.** Replace
   the scalar `Type` cell with a recursive renderer (`render_schema_type` +
   `render_param_detail`) that walks `properties`, `required`, `items`, `enum`, `const`,
   `anyOf`, and `oneOf`:
   - A scalar (`string`/`integer`/`number`/`boolean`/`null`) renders as its type token.
   - An `enum` renders as the back-ticked, comma-joined value list.
   - A `const` renders as `` `=value` ``.
   - `anyOf`/`oneOf` render as `variant | variant | …`; a `[T, null]` pair (the Pydantic
     "optional" shape) collapses to `T (nullable)` so the common case stays readable.
   - An `object` renders its field list as an indented Markdown sub-list under the
     parameter row, each field showing name, rendered type, `required`, and description.
     `array` items recurse the same way.
   - Recursion is bounded by a `max_depth` (default 6) that fails loud
     (`raise ValueError`) if exceeded, rather than silently truncating — an unbounded or
     silently-capped walk would reintroduce the information loss this ADR removes.

2. **Render at least one valid example for the build profile.** `runs.create` and
   `runs.complete_build` are documented with a worked `build_profile` example block per
   source lane, sourced from the same example payloads `runs.profile_examples` returns, so
   the doc and the live tool cannot drift.

3. **Cross-link the provisioning profile to generated examples.** `systems.define.profile`
   and `systems.provision.profile` render their nested fields and link to the
   `systems.profile_examples` tool (which already returns a ready-to-edit example per
   provider), satisfying "render provider/profile variants or link directly to generated
   profile examples."

4. **Add a docs guard that fails on a collapsed structured parameter.** A new test in
   `tests/mcp/core/test_tool_docs.py` walks every tool's parameter schema; if a parameter
   is *structured* (has `properties`, `items`, `enum`, `anyOf`, or `oneOf`) but its
   rendered detail contains none of {field names, enum values, variant separators}, the
   test fails. Legitimately-scalar parameters (`string`, `integer`, …) are exempt, so the
   guard does not false-positive.

The renderer is a cohesive, self-contained function group in `gen_tool_reference.py` so
that a concurrent change to the same script (e.g. maturity-reason rendering) integrates by
composition rather than interleaving.

## Consequences

- Agents see field names, required flags, enums, and union variants directly in the
  reference; `build_profile` carries a copy-pasteable example.
- The generated `*.md` files grow; the `docs-check` CI gate keeps them byte-stable, and
  the new guard prevents regressions back to `any`/`object`/`array`.
- The renderer assumes inlined schemas. If a future tool emits `$ref`/`$defs`, the
  `max_depth` guard will surface it as a loud failure (a `$ref` is an unresolved object
  with no `properties`), prompting an explicit resolver rather than a silent `any`.

## Alternatives considered

- **Render the raw JSON Schema as a fenced code block.** Faithful but unreadable, and it
  buries the field an agent needs under Pydantic boilerplate (`additionalProperties`,
  `minLength`, `default`). Rejected for readability.
- **A fixed depth that silently truncates deep nodes.** Reintroduces the exact
  information loss this ADR removes, with no signal that it happened. Rejected in favor of
  a loud `max_depth`.
- **Resolve `$ref`/`$defs` now.** No tool in the registry emits them today; building a
  resolver for a case that cannot occur is speculative. Deferred behind the loud-failure
  guard.
- **Drop the example blocks and only link to `*.profile_examples`.** The build profile is
  the single most error-prone payload; an inline worked example is worth the few lines.
  Kept the example for `build_profile`, used the link for the provisioning `profile`.
