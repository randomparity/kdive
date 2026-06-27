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
fields. A regression guard catches the two boolean-stringification idioms present in the
tree today (`str(...).lower()` and a bare `"true"`/`"false"` literal) so they cannot return.

## Scope

### In scope — convert to native (see ADR-0263 for the full field list)

- **Counts → `int`** in: `_resource_envelopes` (`vcpus`, `memory_mb`,
  `concurrent_allocation_cap` — see "Capability coercion" below);
  `ops/build_hosts/lifecycle` (`max_concurrent`);
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

### Capability coercion (`_resource_envelopes`)

`resource_capability_data` reads each value via `ResourceCapabilities.scalar(key)`, which
returns `Any` over the JSONB `capabilities` mapping. Today every writer stores the numeric
caps as ints (`register.py` pydantic `int`, `overrides.py` `inst.vcpus`), but dropping
`str()` only yields a native `int` *if* the stored value is already numeric — a non-numeric
stored value would silently pass through as a string and violate the contract. The numeric
caps are therefore coerced explicitly: `int(value)` for `vcpus`/`memory_mb`/
`concurrent_allocation_cap`, and a value that is `None` or not coercible to `int` is dropped
from `data` exactly as the current `is not None` guard already drops a missing key (no new
error path). A test feeds a string-stored capability value and asserts the envelope emits an
`int`. `catalog/shapes._shape_args` reads a typed `SystemShape` (`shape.vcpus` is already
`int`), so it needs no coercion — drop `str()` directly there.

## Behavior contract

| Field class | Before | After |
|---|---|---|
| count (e.g. `match_count`) | `"7"` | `7` |
| flag (e.g. `truncated`, `enabled`) | `"true"` / `"false"` | `true` / `false` |
| `Decimal` money (`estimate_kcu`) | `"1.2500"` | `"1.2500"` (unchanged) |
| UUID / enum | string | string (unchanged) |

`artifacts.get` paging sentinel: callers page until `content_truncated` is `false` (boolean),
not the string `"false"`. The prose that pins the old string type is updated at its source
sites — `catalog/artifacts/registrar.py:90` and `:102` (the `byte_offset` tool-description
text) and `lifecycle/runs/common.py:84` (the console-paging comment) — and
`docs/guide/reference/artifacts.md` is regenerated from the registrar description with
`just docs` (it is generated, not hand-edited). Accepted ADR-0262 keeps its point-in-time
`"false"` wording and is not edited.

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
- Add a coercion test for `_resource_envelopes`: a resource whose JSONB stores `vcpus` as a
  string still yields an `int` in the envelope (or the key is dropped if non-coercible).
- Regenerate the generated reference docs; grep the tree for residual `is "false"` / `is
  "true"` paging prose and confirm none survives outside accepted ADRs.

## Acceptance criteria

1. Every field listed in scope is emitted as native `int`/`bool` in `ToolResponse.data`.
2. `Decimal`, UUID, enum, and `transports` fields are unchanged.
3. The AST guard test passes with no allowlist; it fails if a stringified boolean is
   reintroduced (verified by temporarily reintroducing one).
4. `just ci` is green; the generated reference docs are regenerated and committed.
