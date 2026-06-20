# ADR-0192: Opt-in keyset pagination on the list envelope

Status: Accepted

## Context

Every `*.list` tool caps its result set and gives the caller no way to tell a full
page from a truncated one, nor any way to read past the cap (issue #620, `AX_REVIEW.md`
A3). The single-stream lists (`jobs.list`, `allocations.list`, `systems.list`,
`investigations.list`, `resources.list`, `images.list`, `artifacts.list`) either clamp
to `MAX_LIST_LIMIT` (200) or return every visible row unbounded, with no continuation
marker. An agent that receives exactly 200 rows cannot distinguish "200 rows exist" from
"the first 200 of many".

Two operator-read tools (`audit.query`, `inventory.list`) already bolt a `truncated`
flag onto their `data` dict, so the concept exists but is neither generalized nor
reliable: both fetch exactly `_MAX_ROWS` (500) and set `truncated` when
`len(rows) >= cap`. That heuristic is wrong at the boundary — a result set of exactly
500 rows reports `truncated=true` although nothing was dropped — and the flag is a string
(`"true"`/`"false"`), not a `bool`. Neither offers continuation.

Each list already orders by a stable, total sort key: `(created_at, id)` for the
allocation/system/job/investigation/resource families (`id` a UUID tiebreaker),
`(ts, id)` for `audit.query` (`id` a bigint), and a natural key
(`provider, name, arch`) for `images.list`. `artifacts.list` returns the artifacts of a
single System (a naturally small, system-bounded set). `inventory.list` returns **two**
independent streams (allocations + systems) in one envelope.

ADR-0180 placed lifecycle recovery context in `ToolResponse.data` rather than restructuring
the envelope, and ADR-0170 advertises `data` as an open object whose per-tool keys are
documented in the per-tool docs, not enumerated in the schema. Pagination keys must fit
that same `data`-keyed shape so recovery context and pagination share one payload, and so
no new top-level envelope field (and no `ENVELOPE_OUTPUT_SCHEMA` / drift-guard churn) is
needed.

## Decision

Add opt-in keyset (seek) pagination to the list surface, expressed entirely through the
existing open `ToolResponse.data` payload plus a per-tool `cursor` request parameter. No
new top-level envelope field, no schema change, no migration.

### Response keys (in `data`)

Every paginated `*.list` collection envelope sets:

- `truncated: bool` — `true` iff more rows match than were returned. Real `bool`, not a
  string. Deterministic, never best-effort (see "Truncation detection").
- `next_cursor: str | None` — an opaque continuation token present iff `truncated` is
  `true`; `null` (present-but-null) otherwise. Passing it back as the next call's `cursor`
  returns the next page.
- `total: int` — present **only where it is cheap**, i.e. for an already-bounded,
  single-System collection (`artifacts.list`). Omitted for the open cross-fleet lists,
  whose total would need a second unbounded `COUNT(*)` per call. `count` (the per-page item
  count, set by `ToolResponse.collection`) is unchanged and always present.

### Request key

Each paginated list tool gains an optional `cursor: str | None = None` parameter. Absent or
empty → the first page. Present → resume strictly after the encoded row. A malformed or
wrong-tool cursor is a `configuration_error` (reason `invalid_cursor`), never a silent
first-page fallback — a silent fallback would make an agent re-read page 1 forever.

### Cursor codec

A cursor is an opaque base64url-encoded JSON object `{"t": <tool-tag>, "k": [<key-parts>]}`:

- `t` is the list tool's stable tag (e.g. `"jobs.list"`). Decoding rejects a cursor whose
  tag does not match the calling tool, so a `jobs.list` cursor cannot be replayed against
  `systems.list` (the key shapes differ; a cross-tool replay would otherwise produce a
  malformed predicate or a silently wrong page).
- `k` is the sort key of the **last returned** row, serialized as strings: a timestamp list
  uses `(created_at_iso, id_str)`. The handler rebuilds the keyset predicate
  `(created_at, id) < (%s, %s)` for `DESC, DESC` order (`>` for `ASC`), binding the decoded
  parts as parameters. The codec is the single chokepoint; per-tool handlers only declare
  their sort columns and order direction.

The cursor is **not** a security token and carries no signature. It expresses only "rows
ordered before key K"; project/role scoping is still applied by the same `WHERE` clause on
every page, so a tampered cursor can at most shift the page boundary within rows the caller
may already see. The tool-tag check is integrity for *correctness* (no cross-tool replay),
not authorization.

### Truncation detection (fetch limit+1)

A paginated handler fetches `clamp_list_limit(limit) + 1` rows. If it gets more than
`limit`, it drops the extra row, sets `truncated=true`, and mints `next_cursor` from the
**last kept** (limit-th) row. Otherwise `truncated=false` and `next_cursor=null`. This is
exact: a result set of exactly `limit` rows reports `truncated=false`, fixing the
`>= cap` boundary bug. It replaces the `audit.query` / `inventory.list` `len >= cap`
heuristic.

### Per-tool application

- **`jobs.list`, `allocations.list`, `systems.list`, `investigations.list`,
  `resources.list`** — single stream over `(created_at, id) DESC`. Each gains `cursor` +
  `limit` (the two that lacked an explicit `limit`, `investigations.list` and
  `resources.list`, gain one defaulting to `DEFAULT_LIST_LIMIT`, capped at
  `MAX_LIST_LIMIT`). Full keyset pagination.
- **`images.list`** — single stream over the natural key `(provider, name, arch)`. Gains
  `cursor` + `limit`; the cursor encodes the three-part natural key.
- **`artifacts.list`** — a single-System, already-bounded collection. It gains `truncated`
  (always `false` today, since it returns the whole System's artifacts) and `total` (the
  cheap row count), but **no** `cursor` parameter and no keyset query: the set is small and
  bounded by one System. This keeps the documented contract uniform (`truncated`/`total`
  present everywhere) without inventing a cursor for a set that never needs one.
- **`audit.query`** — single stream over `(ts, id) DESC`. Gains `cursor`; migrates
  `data.truncated` from the string heuristic to the `bool` limit+1 field with a real
  `next_cursor`. The hardcoded `_MAX_ROWS = 500` becomes the limit clamp.
- **`inventory.list`** — a **dual-stream** envelope. It migrates `data.truncated` to the
  `bool` envelope field (true iff *either* stream was truncated) and keeps its existing
  per-stream `allocation_count` / `system_count`, but does **not** gain a single
  `next_cursor` — one cursor cannot resume two independent streams, and inventing two
  cursor parameters for an operator summary tool is not justified by demand. Documented as
  the one non-continuable list; an operator narrows with the `project` / `resource_id`
  filters. This honors AC#3 (migrate off the ad-hoc flag) without over-building.

### Contract documentation

The contract is documented once in `docs/guide/response-envelope.md`, in an additive
"Pagination" subsection of "List responses": the `cursor` request key, the
`truncated` / `next_cursor` / `total` response keys, the follow-the-cursor loop, the
opaque-cursor rule, and the `inventory.list` non-continuable exception.

## Consequences

- An agent can now read a full result set of any size by following `next_cursor` until
  `truncated` is `false`, and can always tell a complete page from a truncated one.
- Truncation is deterministic surface-wide; the exactly-`cap` false positive is gone.
- `audit.query` / `inventory.list` stop emitting the string `truncated` flag; their
  `data.truncated` is now the same `bool` every other list uses. This is a wire change for
  those two tools (string → bool), accepted because the flag was undocumented and these are
  operator tools; the change is called out in the per-tool docstrings.
- Pagination lives entirely in `data`, so `ToolResponse`, `ENVELOPE_OUTPUT_SCHEMA`, and the
  ADR-0170 drift-guard test are untouched. The keys coexist with ADR-0180 recovery keys in
  the same `data` dict with no collision.
- Keyset (not OFFSET) pagination is stable under concurrent inserts: following a cursor
  never skips or repeats a row because a newer row was inserted, since the predicate is
  anchored to a sort-key value, not a row position.
- The cursor codec is a single shared helper in `mcp/tools/_common.py`; a new list tool
  opts in by declaring its sort columns and calling the helper.

## Considered & rejected

- **New top-level envelope fields (`cursor`/`next_cursor`/`truncated` on `ToolResponse`).**
  The issue phrases this as "the top-level envelope", but ADR-0180 already established
  `data` as the home for additive read-context keys and ADR-0170 advertises `data` as open.
  Top-level fields would force `ENVELOPE_OUTPUT_SCHEMA` and the drift-guard test to change,
  and would put pagination keys on *every* response (including non-list and failure
  envelopes) where they are meaningless. Keeping them in `data` is narrower and matches the
  existing `data.truncated` precedent.
- **OFFSET/LIMIT pagination.** Simpler request shape, but unstable under inserts (a row
  added at the head shifts every subsequent page, duplicating or dropping rows) and O(n)
  on deep pages. Keyset over the existing total sort key is stable and index-friendly.
- **Signed/HMAC cursors.** The cursor is not an authorization boundary — every page
  re-applies the same project/role `WHERE` clause — so a signature would add key management
  for no security gain. The tool-tag check covers the only real correctness risk
  (cross-tool replay).
- **A single `next_cursor` for `inventory.list`.** One cursor cannot resume two independent
  streams; encoding both into one token (or adding two cursor params) is unjustified
  complexity for an operator summary that filters by project/resource instead.
- **A `total` on every list.** A correct `total` for the open cross-fleet lists needs a
  second unbounded `COUNT(*)` per call against the same filtered set — not cheap, and not
  required by the acceptance criteria ("`total` where it's cheap"). Emitted only for the
  bounded single-System `artifacts.list`.
- **Silent first-page fallback on a bad cursor.** Returning page 1 for a malformed cursor
  would trap an agent in an infinite re-read of the first page. A bad cursor is a
  `configuration_error` so the agent learns to stop.
