# ADR-0263: emit native JSON numbers/booleans in tool `data` (#863)

- Status: Accepted
- Date: 2026-06-27

## Context

Many MCP tools flatten numeric and boolean response fields to JSON **strings** before
putting them in the `ToolResponse.data` envelope, forcing an agent to coerce
(`int(data["match_count"])`, `data["truncated"] == "true"`) before any arithmetic or
boolean test. The black-box review (`BLACK_BOX_REVIEW.md` §6) named three tools, but an
audit of `src/kdive/mcp/tools/` shows the stringification is a pervasive convention, not
three fields — booleans as `str(x).lower()` / `"true" if c else "false"`, and counts as
`str(count)` — across ~18 tool modules.

The flattening is deliberate, not a serializer limit: `JsonValue`
(`src/kdive/serialization.py`) already admits native `int`/`float`/`bool`, and tools such
as `artifacts.list` already emit `data={"truncated": False, "total": <int>}` natively
(ADR-0192). The advertised output schema is uniform — `advertise_envelope_output_schema`
sets every tool's `outputSchema` to `ENVELOPE_OUTPUT_SCHEMA`, whose `data` is an open
`{"type": "object"}` (ADR-0170). So the wire types of individual `data` fields are **not**
pinned by any per-field schema; correcting them needs no `outputSchema` change.

Two constraints shape what "fix everything" can mean:

- **`Decimal` money/quota values must stay strings.** JSON has no decimal type; the only
  native option is `float`, which loses exact precision for billing. The `accounting.*`
  `*_kcu` / budget / variance fields (`estimate.py`, `usage.py`, `reports.py`) are
  `Decimal` and are stringified on purpose. They stay strings.
- **Audit `args` are not the wire contract.** `str(...)` inside an `audit.*` `args=`
  mapping or a `CategorizedError(details=...)` payload is a separate boundary. Audit args
  are stored only as a one-way digest (`audit.args_digest`), and `args: Mapping[str,
  object]` already accepts native values, so where one dict feeds both a response and an
  audit call (e.g. `accounting.admin` quota, `shapes` define) it can be native for both.

## Decision

Emit native JSON scalars for every count and flag an agent reads out of `ToolResponse.data`
(success or error), and retire the `dict[str, str]` flattening convention for those fields:

- **Counts → `int`**: `resources.list` (`vcpus`, `memory_mb`,
  `concurrent_allocation_cap`), `build_hosts.list` (`max_concurrent`),
  `shapes.*` (`vcpus`, `memory_mb`, `disk_gb`), `artifacts.search_text` (`match_count`),
  `artifacts.get` (`size_bytes`, `next_offset`), `artifacts.fetch_raw` (`size_bytes`,
  `ttl`), upload tools (`expires_in`, `part_number`), `reconcile`/`reconcile_systems`
  counters, `jobs.queue_depth` (`depth_*`), `reports.*` (`count`, `section_count`),
  `accounting.set_quota` (`max_concurrent_*`, `max_pending_allocations`),
  `accounting.report` (`project_count`), `images` prune (`pruned`), `resources.set_capacity`
  (`concurrent_allocation_cap`), `debug.read_memory` (`byte_count`), `introspect.script`
  (`script_bytes`, `max_bytes`).
- **Flags → `bool`**: `build_hosts.list` (`enabled`, `resolves`), `artifacts.search_text`
  (`truncated`), `artifacts.get` (`content_truncated`), `reports.*` (`truncated`,
  `inline_truncated`), `introspect.*` (`truncated`), `diagnostics` (`has_failure`,
  `has_error`), `jobs.pause`/`resume` (`queue_paused`), `resources.deregister` (`forced`),
  `debug.stop` (`timed_out`).

Helper return types that carried these fields change from `dict[str, str]` to
`dict[str, JsonValue]` (`resource_capability_data`, `shapes._shape_args`,
`debug.ops._stop_data`, `accounting.admin` quota `values`). Where a dict is shared with an
audit call, the same native dict feeds both; the audit digest input changes (a one-way
hash with no stored plaintext to compare against) but audit behavior does not.

**Out of scope, kept as strings:** `Decimal` money/quota (`*_kcu`, budget, variance,
`limit_kcu`); UUIDs and enum values; `resources.list` `transports` (a list flattened to a
comma-joined string — a list-flattening concern, not a number/boolean); audit-`args`/error-
`details` `str(...)` that never reaches response `data`.

**Documentation.** `artifacts.get`'s paging sentinel flips from the string `"false"` to the
boolean `false`; the tool-description and reference prose that says "page until
`content_truncated` is `\"false\"`" is updated to `false`, and the generated reference
(`just docs`) is regenerated. Accepted ADRs (e.g. ADR-0262) are not edited in place.

**Guardrail.** A regression test (`tests/mcp/test_no_stringified_flags.py`) AST-walks
`src/kdive/mcp/tools/` and fails on the two unambiguous boolean-stringification idioms — a
`str(...).lower()` call and a bare `"true"`/`"false"` string constant used as a `dict`
value or an `if`/`else` expression branch. After this change the tree has zero occurrences,
so the guard runs allowlist-free. Numeric regressions are not statically distinguishable
from legitimate `str(uuid)`, so they are covered by per-tool `isinstance(..., int)`
assertions rather than the AST guard; the guard's scope limit is recorded in its module
docstring.

## Consequences

- An agent reads `data["match_count"] + 1` or `if data["truncated"]:` directly; no coercion
  layer, no `== "true"` string compares.
- A future stringified boolean flag fails the AST guard at commit time; a stringified count
  is caught by the touched tool's strengthened type assertion.
- `Decimal` money fields keep exact precision; the int/bool fix never touches them.
- The change is wire-visible: a client that parsed `structured_content` and compared
  `data["enabled"] == "true"` or `int(data["max_concurrent"])` must read the native value.
  No in-repo consumer does (tests are updated alongside); external clients on these fields
  must adjust. This is the one-time cost of retiring the convention.
- No schema, migration, RBAC, or config change; `outputSchema` stays the uniform open-`data`
  envelope.

## Considered & rejected

- **Fix only the three tools the review named.** Leaves the identical anti-pattern in ~15
  other tools for a future issue and keeps the `dict[str, str]` convention alive; the issue
  explicitly asks whether to kill the convention wholesale. Rejected for a complete sweep.
- **Also convert `Decimal` money/quota fields to JSON numbers.** `float` loses exact
  billing/quota precision; there is no native JSON decimal. Money stays string.
- **Convert `transports` to a JSON array in the same change.** It is a list flattened to a
  comma string, not a number/boolean; a separate concern outside this issue's scope.
- **A runtime guardrail that invokes every tool and type-checks each field.** Needs full DB
  fixtures for every tool and a central field/type registry; the AST guard (bools) plus
  per-tool type assertions (counts) give the same protection without that machinery.
- **A blanket source ban on every `str(...)` in `tools/`.** Indistinguishable from the
  legitimate `str(uuid)` / `str(enum)` / `Decimal` cases; would force a large allowlist.
  The guard targets only the unambiguous boolean idioms.
