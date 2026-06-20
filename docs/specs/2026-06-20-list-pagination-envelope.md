# Spec: opt-in keyset pagination on the list envelope

Issue: #620 (`AX_REVIEW.md` A3) · ADR: [ADR-0192](../adr/0192-list-pagination-envelope.md)

## Goal

Make every `*.list` result set fully retrievable and its truncation deterministically
detectable, through one generalized pagination contract carried in the existing open
`ToolResponse.data` payload. Migrate the two ad-hoc `data.truncated` tools onto that
contract.

## Acceptance criteria (from the issue)

1. A result set larger than the cap is fully retrievable by following `next_cursor`.
2. Truncation is deterministically detectable (`truncated` is reliable, not best-effort).
3. `audit.query` / `inventory.list` migrate to the envelope field instead of their ad-hoc
   `data` flag.

## Contract

### Request

A paginated list tool gains an optional `cursor: str | None = None` parameter, plus a
`limit: int` (defaulting to `DEFAULT_LIST_LIMIT = 50`, clamped to `MAX_LIST_LIMIT = 200`)
for the two single-stream lists that lacked one (`investigations.list`, `resources.list`).

- `cursor` absent / empty string → first page.
- `cursor` present → page strictly after the encoded row, in the tool's sort order.
- `cursor` malformed, or minted by a different list tool → `configuration_error` with
  `data.reason = "invalid_cursor"`. Never a silent first-page fallback.

### Response (`data` keys)

- `truncated: bool` — `true` iff more matching rows exist than were returned.
- `next_cursor: str | None` — opaque token, present (non-null) iff `truncated` is `true`;
  `null` otherwise.
- `total: int` — present only for `artifacts.list` (cheap, single-System bounded set).
- `count: int` — unchanged; the per-page item count set by `ToolResponse.collection`.

### Cursor codec (single shared helper in `mcp/tools/_common.py`)

`encode_cursor(tool_tag, key_parts) -> str` produces base64url(JSON
`{"t": tool_tag, "k": [str, ...]}`). `decode_cursor(tool_tag, cursor) -> list[str] | None`
returns the key parts, or raises a sentinel that the caller maps to the `invalid_cursor`
`configuration_error` when:

- the token is not valid base64url / not the expected JSON object,
- `t` does not equal the calling tool's tag,
- `k` is not a list of the expected arity for that tool.

The key parts are the **last returned row's** sort key serialized as strings:
`(created_at.isoformat(), str(id))` for the timestamp lists, `(provider, name, arch)` for
`images.list`, `(ts.isoformat(), str(id))` for `audit.query`.

### Keyset query (fetch limit+1)

Each single-stream handler:

1. Clamps `limit`; fetches `limit + 1` rows ordered by the tool's total sort key.
2. If a `cursor` is supplied, decodes it and adds the seek predicate to the `WHERE`. The
   predicate is a row-value comparison `(col_a, col_b) < (%s, %s)` (for `DESC, DESC`) or
   `(col_a, col_b) > (%s, %s)` (for `ASC, ASC`). The decoded string parts bind as
   parameters (a timestamp part parses back to `timestamptz`; the tiebreaker binds as
   text/uuid per column type — psycopg casts against the typed column).
3. If more than `limit` rows came back, drop the extra, set `truncated=true`, and
   `next_cursor = encode_cursor(tag, last_kept_row_key)`. Else `truncated=false`,
   `next_cursor=null`.

**The keyset predicate is only valid when every column in the sort key sorts the same
direction.** A row-value comparison `(a, b) < (x, y)` is equivalent to the seek
"strictly before `(x, y)` in `a DESC, b DESC`" only when *both* columns are `DESC`; for a
mixed `a DESC, b ASC` order the correct seek is `a < x OR (a = x AND b > y)`, which the
tuple form does **not** express. Rather than generate a per-column-direction predicate,
this design **normalizes the tiebreaker to match the leading column** so a single
uniform-direction tuple predicate is always correct:

- `allocations.list` and `systems.list` order `created_at DESC, id` today (id implicitly
  ASC — a *mixed* order). They are changed to `created_at DESC, id DESC`. This is a benign
  change: `id` is a unique UUID, so `id DESC` is still a total tiebreaker and only reorders
  rows that share an exact `created_at` microsecond. `jobs.list`, `investigations.list`,
  and `audit.query` already order `… DESC, id DESC`.
- `resources.list` orders `created_at, id` (both ASC) — uniform, no change; the seek uses
  `>`.
- `images.list` orders `provider, name, arch` (all ASC) — uniform, no change.

A timestamp encoded into a cursor must round-trip to the **exact stored value** (tz-aware,
full microsecond precision); `datetime.isoformat()` preserves microseconds and the seek
binds the parsed value against the `timestamptz` column, so the `a = x` arm matches the
boundary row exactly. A round-trip test per list type seeds rows that share one
`created_at` microsecond and asserts following the cursor reads every row exactly once with
no skip or repeat across the tie boundary.

## Per-tool work

| Tool | Sort key | Gains `cursor`? | Gains `limit`? | `total`? | Notes |
|---|---|---|---|---|---|
| `jobs.list` | `(created_at,id) DESC` | yes | already has | no | `queue.recent_jobs` gains a cursor predicate |
| `allocations.list` | `(created_at,id) DESC` | yes | already has | no | ORDER BY changes `id`→`id DESC` (uniform-direction seek) |
| `systems.list` | `(s.created_at,s.id) DESC` | yes | already has | no | joined query; ORDER BY changes `s.id`→`s.id DESC` |
| `investigations.list` | `(created_at,id) DESC` | yes | **new** | no | |
| `resources.list` | `(created_at,id) DESC` | yes | **new** | no | was unbounded |
| `images.list` | `(provider,name,arch) ASC` | yes | **new** | no | natural-key cursor |
| `artifacts.list` | n/a | **no** | no | **yes** | bounded single-System; `truncated` always `false` |
| `audit.query` | `(ts,id) DESC` | yes | clamp replaces `_MAX_ROWS` | no | string→bool migration |
| `inventory.list` | dual | **no** | clamp replaces `_MAX_ROWS` | no | bool field; non-continuable, narrow with filters |

## Edge cases (each gets a test)

- **Empty result set** → `truncated=false`, `next_cursor=null`, `count=0`, items empty.
- **Exactly `limit` rows** → `truncated=false`, `next_cursor=null` (the limit+1 fetch
  returns exactly `limit`, no extra). This is the boundary the old `>= cap` heuristic got
  wrong.
- **`limit + 1` matching rows** → first page `truncated=true` + `next_cursor`; following it
  returns the final row with `truncated=false`.
- **Full drain across pages** → following `next_cursor` repeatedly reads every row exactly
  once, in order, with no duplicate and no gap (round-trip test seeds > cap rows).
- **Malformed cursor** (`"!!!"`, truncated base64, non-JSON) → `invalid_cursor`
  `configuration_error`.
- **Cross-tool cursor** (a `jobs.list` cursor passed to `systems.list`) → `invalid_cursor`.
- **`limit=0` / negative** → clamped to 1 (existing `clamp_list_limit`).
- **`limit` above `MAX_LIST_LIMIT`** → clamped to 200.
- **Cursor on the same `created_at` microsecond** → the `id` tiebreaker keeps the order
  total; the page boundary lands between the two tied rows, never dropping one.
- **`inventory.list` either stream at cap** → `truncated=true`; both under cap →
  `truncated=false`.
- **`audit.query` / `inventory.list` wire change** → `data.truncated` is a JSON `bool`, not
  the string `"true"`/`"false"`; existing tests updated. A grep for readers of these tools'
  `data.truncated` confirms only the two handlers + their tests read it (the other
  `truncated` keys in the codebase — `introspect`, `artifacts.get` `content_truncated`,
  `artifacts_search` — are unrelated content-truncation flags on different tools and stay
  as-is). The two per-tool reference docs (`docs/guide/reference/audit.md`,
  `docs/guide/reference/inventory.md`) and the tool docstrings are updated to describe the
  bool field; if those reference docs are generated, they are regenerated.

## Non-goals

- `runs.list` (#623) and server-side jobs/allocations filters (#621) are sibling issues
  that build on this envelope; not implemented here.
- No new top-level `ToolResponse` field; no `ENVELOPE_OUTPUT_SCHEMA` / drift-guard change;
  no DB migration.
- No signed cursors; no OFFSET pagination; no `total` for the open fleet lists; no
  `next_cursor` for `inventory.list`.

## Guardrails

`just lint`, `just type` (whole tree), `just test` per the justfile; `just ci` before
push. CI also gates `just docs-check` and `just adr-status-check` **individually** (not via
`just ci`), so after editing any tool docstring run `just docs` and commit the regenerated
`docs/guide/reference/*.md`. Tests live beside the existing list-tool tests under
`tests/mcp/`. The cursor codec gets a focused unit test (`tests/mcp/core/` or
`tests/mcp/test__common.py`).
