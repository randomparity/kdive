# Native JSON numbers/booleans in MCP tool `data` (#863)

- Issue: #863
- ADR: [ADR-0263](../adr/0263-native-json-scalars-in-tool-data.md)
- Status: Draft

## Problem

MCP tools flatten numeric and boolean response fields to JSON **strings** in the
`ToolResponse.data` envelope, so an agent must coerce (`int(data["match_count"])`,
`data["truncated"] == "true"`) before arithmetic or a boolean test. The black-box review
named three tools; an audit of `src/kdive/mcp/tools/` shows the stringification is a
convention spanning ~18 modules.

`JsonValue` already admits native `int`/`float`/`bool`, the advertised `outputSchema` keeps
`data` an open object (ADR-0170), and `artifacts.list` already emits native scalars — so the
stringification is deliberate flattening, correctable without any schema change.

## Goal

Every count and flag an agent reads out of `ToolResponse.data` (success or error) is a
native JSON `int`/`bool`. The `dict[str, str]` flattening convention is retired for those
fields. A regression guard prevents reintroducing stringified booleans.

## Scope

### In scope — convert to native (see ADR-0263 for the full field list)

- **Counts → `int`** in: `_resource_envelopes` (`vcpus`, `memory_mb`,
  `concurrent_allocation_cap`); `ops/build_hosts/lifecycle` (`max_concurrent`);
  `catalog/shapes` (`vcpus`, `memory_mb`, `disk_gb`); `catalog/artifacts/reads`
  (`match_count`, `size_bytes`, `next_offset`); `catalog/artifacts/raw_fetch` (`size_bytes`,
  `ttl`); `catalog/artifacts/uploads` (`expires_in`, `part_number`); `ops/reconcile` and
  `ops/reconcile_systems` (all counters); `ops/queue` (`depth_*`); `reports/generate`
  (`count`, `section_count`); `accounting/admin` (`max_concurrent_*`,
  `max_pending_allocations`); `accounting/reports` (`project_count`); `ops/images/retention`
  (`pruned`); `ops/tuning` (`concurrent_allocation_cap`); `debug/ops` (`byte_count`);
  `debug/introspect` (`script_bytes`, `max_bytes`).
- **Flags → `bool`** in: `ops/build_hosts/lifecycle` (`enabled`, `resolves`);
  `catalog/artifacts/reads` (`truncated`, `content_truncated`); `reports/generate`
  (`truncated`, `inline_truncated`); `debug/introspect` (`truncated`); `ops/diagnostics`
  (`has_failure`, `has_error`); `ops/queue` (`queue_paused`); `ops/resources/deregister`
  (`forced`); `debug/ops` (`timed_out`).

### Out of scope — stays string

- `Decimal` money/quota (`accounting.*` `*_kcu`, budget, variance, `limit_kcu`) — `float`
  loses precision.
- UUIDs, enum values, `resources.list` `transports` (list→comma string).
- `str(...)` confined to audit `args=` / error `details=` that never reaches response `data`.

## Behavior contract

| Field class | Before | After |
|---|---|---|
| count (e.g. `match_count`) | `"7"` | `7` |
| flag (e.g. `truncated`, `enabled`) | `"true"` / `"false"` | `true` / `false` |
| `Decimal` money (`estimate_kcu`) | `"1.2500"` | `"1.2500"` (unchanged) |
| UUID / enum | string | string (unchanged) |

`artifacts.get` paging sentinel: callers page until `content_truncated` is `false` (boolean),
not the string `"false"`. Tool-description/reference prose is updated accordingly and the
generated reference regenerated with `just docs`.

Shared dicts that feed both a response and an audit call (`accounting.admin` quota `values`,
`catalog/shapes._shape_args`) become native and serve both; audit args accept
`Mapping[str, object]` and store only a one-way digest, so audit behavior is unchanged.

## Guardrail

`tests/mcp/test_no_stringified_flags.py` AST-walks `src/kdive/mcp/tools/` and fails if it
finds either unambiguous boolean-stringification idiom: a `str(...).lower()` call, or a bare
`"true"`/`"false"` string constant used as a `dict` value or an `if`/`else` expression
branch. After the change the tree is clean, so the guard runs without an allowlist. Numeric
stringification is statically indistinguishable from `str(uuid)`; per-tool
`isinstance(..., int)` assertions cover counts instead.

## Tests

- Strengthen each touched tool's existing unit test to assert the native type
  (`isinstance(data["match_count"], int)`, `data["truncated"] is True`, etc.) rather than the
  old string equality.
- Add the AST guard test (fails before the sweep on the existing idioms; passes after).
- Regenerate the generated reference docs; confirm no other reference prose pins the old
  string types.

## Acceptance criteria

1. Every field listed in scope is emitted as native `int`/`bool` in `ToolResponse.data`.
2. `Decimal`, UUID, enum, and `transports` fields are unchanged.
3. The AST guard test passes with no allowlist; it fails if a stringified boolean is
   reintroduced (verified by temporarily reintroducing one).
4. `just ci` is green; the generated reference docs are regenerated and committed.
