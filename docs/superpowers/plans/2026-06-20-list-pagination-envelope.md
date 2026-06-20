# Plan: opt-in keyset pagination on the list envelope (#620)

Spec: [docs/specs/2026-06-20-list-pagination-envelope.md](../../specs/2026-06-20-list-pagination-envelope.md)
ADR: [ADR-0192](../../adr/0192-list-pagination-envelope.md)

This plan is executed directly in one session (the tasks are tightly coupled around one
shared codec + a uniform per-tool edit), TDD throughout. Guardrails for every commit:
`just lint && just type && just test` (whole-tree type check); before push `just ci` plus
`just docs-check` and `just adr-status-check` (CI gates these individually). After editing a
tool docstring, run `just docs` and commit the regenerated `docs/guide/reference/*.md`.

Conventions (from CLAUDE.md / AGENTS.md): absolute imports only; ≤100 lines / complexity ≤8
per function; Google-style docstrings on non-trivial public APIs; pick the most specific
`ErrorCategory`; return `ToolResponse` from the boundary; never invent error strings; doc
prose stays plain (no "robust"/"comprehensive"/"critical").

## Task 1 — Cursor codec + pagination helper in `_common.py`

**Where it fits:** the single shared chokepoint every paginated list calls (ADR-0192
"Cursor codec"). All later tasks depend on it.

**Files:** `src/kdive/mcp/tools/_common.py`; new test
`tests/mcp/test__common_pagination.py` (or extend an existing `_common` test if present —
check `tests/mcp/` first).

**Implement:**
- `class InvalidCursor(Exception)` — internal sentinel raised by `decode_cursor`.
- `encode_cursor(tool_tag: str, key_parts: Sequence[str]) -> str` — `base64.urlsafe_b64encode`
  of `json.dumps({"t": tool_tag, "k": [str, ...]})`, ASCII, no padding stripping needed
  (keep standard padding; decode must accept it).
- `decode_cursor(tool_tag: str, cursor: str, *, arity: int) -> list[str]` — urlsafe-b64
  decode → json load → validate it is a dict with `t == tool_tag` and `k` a list of exactly
  `arity` strings; otherwise raise `InvalidCursor`. Catch `binascii.Error`,
  `UnicodeDecodeError`, `json.JSONDecodeError`, `ValueError`, `TypeError` → `InvalidCursor`.
- `invalid_cursor_error(object_id: str) -> ToolResponse` — a `configuration_error` whose
  `data.reason = "invalid_cursor"`. Add `INVALID_CURSOR = "invalid_cursor"` to
  `ConfigErrorReason` and build via `config_error_reason`.
- `paginate(rows: list[T], limit: int) -> tuple[list[T], bool]` — given `limit + 1` fetched
  rows and the clamped `limit`, return `(rows[:limit], len(rows) > limit)`.
- Export the new public names in `__all__`.

**Tests (write first, watch fail):**
- round-trip: `decode_cursor(t, encode_cursor(t, parts), arity=len(parts)) == parts`.
- wrong tag → `InvalidCursor`.
- wrong arity → `InvalidCursor`.
- malformed (`"!!!"`, non-b64, b64-of-non-json, b64-of-json-array) → `InvalidCursor`.
- `paginate`: `limit+1` rows → `(limit rows, True)`; exactly `limit` → `(rows, False)`;
  empty → `([], False)`.
- `invalid_cursor_error` builds an `error` envelope, category `configuration_error`,
  `data.reason == "invalid_cursor"`.

**Acceptance:** codec round-trips; every malformed/cross-tool/arity case raises
`InvalidCursor`; `paginate` boundary correct at exactly-`limit`.

## Task 2 — Single-stream timestamp lists: jobs, allocations, systems, investigations, resources

**Where it fits:** the bulk of the list surface; uses Task 1.

**Files (handler + its test each):**
- `src/kdive/jobs/queue.py` (`recent_jobs`) + `src/kdive/mcp/tools/catalog/jobs.py`
  (`list_jobs`, `jobs_list` wrapper) + `tests/mcp/catalog/test_jobs_tools.py`
- `src/kdive/mcp/tools/lifecycle/allocations/view.py` + its test
- `src/kdive/mcp/tools/lifecycle/systems/view.py` + `tests/mcp/lifecycle/test_systems_tools.py`
- `src/kdive/mcp/tools/catalog/investigations.py` + `tests/mcp/catalog/test_investigations_tools.py`
- `src/kdive/mcp/tools/catalog/resources.py` + `tests/mcp/catalog/test_resources_tools.py`

**Per handler:**
1. Add an optional `cursor: str | None = None` parameter to the handler and its tool
   wrapper (`Annotated[str | None, Field(description="Opaque continuation cursor from a
   prior page's next_cursor; omit for the first page.")]`).
2. For `investigations.list` and `resources.list`, add `limit: int = DEFAULT_LIST_LIMIT`
   (tool wrapper `Field(description="Maximum rows returned (capped at 200).")`) and apply
   `clamp_list_limit`.
3. Normalize ORDER BY to uniform direction:
   - allocations: `created_at DESC, id` → `created_at DESC, id DESC`.
   - systems: `s.created_at DESC, s.id` → `s.created_at DESC, s.id DESC`.
   - resources: `created_at, id` (already uniform ASC) — keep.
   - jobs/investigations: already `… DESC, id DESC` — keep.
4. Fetch `clamped + 1` rows. When `cursor` is supplied, `decode_cursor(tag, cursor,
   arity=2)`; on `InvalidCursor` return `invalid_cursor_error(object_id)`. Add the seek
   predicate to the WHERE: `(created_at, id) < (%s, %s)` for DESC lists,
   `(created_at, id) > (%s, %s)` for resources. **Bind a real typed value, not the raw
   cursor string**: parse the timestamp part with `datetime.fromisoformat(...)` and the id
   with `UUID(...)` before binding, so psycopg sends a `timestamptz` / `uuid` (a raw text
   bind inside a row-value comparison forces Postgres to infer the cast, which is fragile;
   a typed bind is unambiguous). A parse failure here is also an `invalid_cursor` config
   error (a well-formed envelope can still carry a non-timestamp/non-uuid `k` part), so wrap
   the parse and map `ValueError` to `invalid_cursor_error`.
5. `kept, truncated = paginate(rows, clamped)`. Build `next_cursor`: if `truncated`,
   `encode_cursor(tag, (kept[-1].created_at.isoformat(), str(kept[-1].id)))`, else `None`.
6. Pass `data={"truncated": truncated, "next_cursor": next_cursor}` to
   `ToolResponse.collection` (merged with any existing data the handler sets). `count` stays
   auto-set by `collection`.

For `queue.recent_jobs`, add a `cursor_key: tuple[str, str] | None = None` parameter (the
decoded parts) and the seek predicate inside the query; the codec decode/encode stays in the
tool layer (`jobs.py`) so `queue` has no MCP dependency. Fetch `limit + 1`.

**Tests (write first):** for each list — empty → `truncated=False`, `next_cursor` null,
`count=0`; exactly `limit` rows → `truncated=False`, null cursor; `limit+1` rows → first page
`truncated=True` + cursor, following the cursor returns the remaining row(s) with
`truncated=False`; a full multi-page drain seeded above the cap reads every row once in order
(no dup/gap); rows sharing one `created_at` microsecond paginate across the tie with no
skip/repeat; malformed cursor → `invalid_cursor` config error; a `jobs.list` cursor passed to
`systems.list` → `invalid_cursor`. Reuse the existing test seeds (`tests/mcp/_seed.py`).

**Acceptance:** each list is fully drainable by following `next_cursor`; truncation exact at
the boundary; project/role scoping unchanged (the seek only ANDs onto the existing WHERE).

## Task 3 — `images.list` (natural-key cursor)

**Files:** `src/kdive/mcp/tools/catalog/images.py` + `tests/mcp/ops/test_images_tools.py`.

Same shape as Task 2 but:
- Sort key is `(provider, name, arch)` ASC; cursor arity 3; seek `(provider, name, arch) >
  (%s, %s, %s)`; all three bind as text.
- Add `limit: int = DEFAULT_LIST_LIMIT` + `cursor`; the query was unbounded — add
  `LIMIT %s` with `clamped + 1`.
- `next_cursor` parts = `(row.provider, row.name, row.arch)` of the last kept row.

**Tests:** empty, exactly-limit, limit+1 + follow, malformed cursor, cross-tool cursor.

**Acceptance:** images list drainable by natural-key cursor; truncation exact.

## Task 4 — `artifacts.list` (`truncated` + `total`, no cursor)

**Files:** `src/kdive/mcp/tools/catalog/artifacts/reads.py` + `tests/mcp/catalog/test_artifacts_tools.py`.

`artifacts_list` returns a single System's redacted artifacts (bounded). Add
`data={"truncated": False, "total": len(items)}` to the collection. No `cursor` parameter,
no keyset query, no `limit`. This keeps the documented response keys uniform.

**Tests:** a System with N artifacts → `data.total == N`, `data.truncated is False`,
`count == N`; empty System → `total == 0`, `truncated False`.

**Acceptance:** artifacts.list carries the uniform `truncated`/`total` keys without a cursor.

## Task 5 — Migrate `audit.query` off the string flag

**Files:** `src/kdive/mcp/tools/ops/audit.py` + `tests/mcp/ops/test_audit_query.py`.

- Add `cursor: str | None = None` to the `_AuditQueryFilters` model? No — keep it a separate
  parameter on the tool wrapper + `query` dispatch, since the discriminated-union request
  model has `extra="forbid"`. Add `cursor` to the wrapper signature and thread it through
  `query` → `query_project` / `query_all_projects` → `_query_*` → `_fetch_rows`.
- Replace the hardcoded `_MAX_ROWS = 500` fetch with `clamp_list_limit(limit)` where `limit`
  defaults to `DEFAULT_LIST_LIMIT`; add a `limit` tool parameter. Fetch `clamped + 1`.
- Sort key `(ts, id)` DESC (already `ORDER BY ts DESC, id DESC`); arity-2 cursor; seek
  `(ts, id) < (%s, %s)`; bind `datetime.fromisoformat(ts_part)` and `int(id_part)` (id is a
  bigint) — typed binds, not raw strings; a parse failure → `invalid_cursor_error`.
- **Decode the cursor after the authz check (and, for the all-projects scope, after the
  read-audit record), before `_fetch_rows`.** The authz denial path (and its
  `platform_audit_log` record) must be reached identically whether or not a cursor is
  present, so an unauthorized caller cannot use a malformed cursor to change the denial-audit
  behavior. Concretely: in `_query_project` / `_query_cross_project`, after
  `require_role`/`require_platform_role` (and `record_read` for cross-project), decode the
  cursor; on `InvalidCursor`/parse `ValueError` return `invalid_cursor_error`, then call
  `_fetch_rows` with the typed seek key. A bad cursor is the caller's own input error,
  surfaced only after they pass authz.
- `_response` takes `(kept, truncated, next_cursor)` and sets `data={"truncated": <bool>,
  "next_cursor": <str|None>}` — drop the `"true"/"false"` string.
- Update the docstring (drives the generated reference doc). Run `just docs`.

**Tests:** update existing string-flag assertions to bool; add cursor follow + exact-boundary
+ malformed-cursor cases. Keep the existing authz/audit-record tests intact (the seek must AND
onto the same scoped WHERE; cross-project read-audit still recorded).

**Acceptance:** audit.query paginates by `(ts,id)` cursor; `data.truncated` is a bool;
the read-audit / authz behavior is unchanged.

## Task 6 — Migrate `inventory.list` off the string flag (no cursor)

**Files:** `src/kdive/mcp/tools/ops/inventory.py` + `tests/mcp/ops/test_inventory_list.py`.

- Replace the hardcoded `_MAX_ROWS = 500` with `clamp_list_limit(limit)`; add a `limit` tool
  parameter (defaults `DEFAULT_LIST_LIMIT`). Each stream fetches `clamped + 1`.
- `_response` computes `alloc_trunc = paginate(allocations, clamped)[1]`,
  `sys_trunc = paginate(systems, clamped)[1]`, slices both to `clamped`, and sets
  `data={"truncated": alloc_trunc or sys_trunc, "allocation_count": <int>,
  "system_count": <int>}` — drop the string flag; make the counts ints (currently strings).
  No `next_cursor`.
- Update the docstring (non-continuable, narrow with filters). Run `just docs`.

**Tests:** update string-flag + string-count assertions to bool/int; either-stream-at-cap →
`truncated True`; both under → `False`. Keep authz/read-audit tests intact.

**Acceptance:** inventory.list `data.truncated` is a bool true iff either stream truncated;
counts are ints; non-continuable as documented; authz unchanged.

## Task 7 — Document the contract once + regenerate references

**Files:** `docs/guide/response-envelope.md` (additive **Pagination** subsection under
"List responses" — sibling agent #619 also edits this file's idempotency section, so keep the
edit confined to a new pagination subsection); regenerate `docs/guide/reference/*.md` via
`just docs`.

Document: the `cursor` request key; the `truncated` / `next_cursor` / `total` response keys;
the follow-the-cursor loop (call until `truncated` is false); cursors are opaque (do not
parse/construct), tool-specific (a cursor from one list is rejected by another →
`configuration_error`), and not security tokens; `inventory.list` is the one non-continuable
list (narrow with `project`/`resource_id`).

**Acceptance:** `just docs-check` passes (committed reference matches a fresh generation);
the envelope doc describes the full contract; no edit outside the new pagination subsection.

## Verification (before push)

- `just lint && just type && just test` green.
- `just ci` green; `just docs-check` and `just adr-status-check` green.
- The output-schema drift-guard test (`tests/mcp/core/test_output_schema.py`) still passes
  untouched — confirms no top-level `ToolResponse` field was added (pagination lives in
  `data`).
- Grep `rg '"truncated": "(true|false)"' src/kdive/mcp/tools` returns nothing (both string
  flags removed); other `truncated` content flags (introspect, content_truncated,
  artifact_search) are untouched.

## Rollback / cleanup

Pure additive on the wire except the two `data.truncated` string→bool flips (called out in
the ADR/docstrings). No migration, so rollback is reverting the branch. The ORDER BY
normalization on allocations/systems is behavior-preserving except for tie ordering within
one `created_at` microsecond.
