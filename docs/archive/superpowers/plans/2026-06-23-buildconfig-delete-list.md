# Plan — buildconfig.delete + buildconfig.list (#751)

Derived from `docs/specs/2026-06-23-buildconfig-delete-list.md` and ADR-0231.

This is a tightly-coupled, two-file feature (data layer + one tool module + one exposure
map), implemented directly in the session with TDD — not split across independent
implementer subagents. Guardrails for every commit: `just lint`, `just type`, and the
focused tests (`uv run python -m pytest tests/mcp/catalog/test_build_configs_tool.py
tests/domain/catalog/ -q`). Full `just test` once before first push.

## Where this fits

`buildconfig.get`/`buildconfig.set` exist; this adds the missing `list`/`delete` to reach
parity with `images.*`/`shapes.*`. No migration: `build_config_catalog` and its `source`
column already exist.

## Task 1 — data layer: `list_build_configs`

**File:** `src/kdive/build_configs/catalog.py`

Add `async def list_build_configs(conn: AsyncConnection) -> list[BuildConfigEntry]`:
`SELECT name, object_key, sha256, description, source FROM build_config_catalog ORDER BY
name`, map each row through the existing `parse_build_config_row`.

**TDD (tests in `tests/domain/catalog/` or the existing build-config test module):**
- empty catalog → `[]`.
- after seeding + one operator upsert + one config upsert → three entries sorted by name,
  each carrying the right `source`.

**Acceptance:** returns every row, sorted by `name`, reusing `BuildConfigEntry`.

## Task 2 — data layer: `delete_operator_build_config`

**File:** `src/kdive/build_configs/catalog.py`

Add a small frozen result type and the delete function:

```
class BuildConfigDeleteOutcome(StrEnum):  # or a small frozen dataclass
    DELETED, NOT_OPERATOR, NOT_FOUND
```

`async def delete_operator_build_config(conn, name) -> tuple[outcome, source|None]`:
- `DELETE FROM build_config_catalog WHERE name = %(name)s AND source = 'operator'
  RETURNING name` — if a row comes back, outcome = DELETED.
- If nothing was deleted, read provenance (`read_build_config_provenance` or an inline
  `SELECT source`) to distinguish NOT_FOUND (no row) from NOT_OPERATOR (row exists,
  source != 'operator'); return the actual source for the reason payload.
- Both statements run on the caller's connection inside the caller's transaction; the
  caller holds the per-name advisory lock so this is race-free against a concurrent `set`.

**TDD:**
- delete an operator row → DELETED; row gone.
- delete a seed row → NOT_OPERATOR with source='seed'; row still present.
- delete a config row → NOT_OPERATOR with source='config'; row still present.
- delete an unknown name → NOT_FOUND.

**Acceptance:** removes only `source='operator'`; reports the three outcomes with the actual
source.

## Task 3 — tool: `buildconfig.list`

**File:** `src/kdive/mcp/tools/catalog/build_configs.py`

- `_LIST_TOOL = "buildconfig.list"`.
- `async def list_build_config_entries(pool, ctx) -> ToolResponse`: authenticated-only (no
  RBAC), `bind_context(principal=...)`, call `list_build_configs`, build a
  `ToolResponse.collection` of per-row sub-envelopes carrying `name`, `sha256`, `source`,
  `description` (no bytes). Mirror `list_shapes`.
- Register in `register()` with `_docmeta.read_only()`, `meta={"maturity":"implemented"}`,
  reading `current_context()`.

**TDD (tests/mcp/catalog/test_build_configs_tool.py):** seed + operator + config → three
sub-envelopes sorted by name, each with the four fields and no `content`.

## Task 4 — tool: `buildconfig.delete`

**File:** `src/kdive/mcp/tools/catalog/build_configs.py`

- `_DELETE_TOOL = "buildconfig.delete"`.
- `async def delete_build_config(pool, ctx, *, name) -> ToolResponse`, mirroring
  `set_build_config`'s gate/audit shape:
  1. `require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)`; on `AuthorizationError`,
     `audit_platform_denial(...)` + `AUTHORIZATION_DENIED` failure envelope
     (`suggested_next_actions=[_DELETE_TOOL]`).
  2. Open `pool.connection()` + `conn.transaction()` +
     `advisory_xact_lock(conn, LockScope.BUILD_CONFIG, name)`.
  3. Call `delete_operator_build_config`. Map outcome:
     - NOT_FOUND → `CONFIGURATION_ERROR`, `data={"reason":"not_found","name":name}`,
       `suggested_next_actions=[_LIST_TOOL]`; **no audit row**.
     - NOT_OPERATOR → `CONFIGURATION_ERROR`,
       `data={"reason":"not_operator_source","source":source,"name":name}`,
       `suggested_next_actions=[_LIST_TOOL]`; **no audit row**.
     - DELETED → `audit.record_platform(...)` (tool=_DELETE_TOOL, scope=name), then success
       `ToolResponse.success(name, "deleted", suggested_next_actions=[_LIST_TOOL, _SET_TOOL])`.
- Register with `_docmeta.mutating()` (catalog removal, not a live-resource teardown — the
  `shapes.delete` precedent, which is `mutating()` and not in `DESTRUCTIVE_TOOLS`).

**TDD:**
- operator delete → `deleted`; `get` now CONFIGURATION_ERROR; `list` excludes it; exactly
  one `buildconfig.delete` audit row.
- seed delete → CONFIGURATION_ERROR + reason=not_operator_source + source=seed; row present;
  no audit row.
- config delete → same with source=config.
- unknown delete → CONFIGURATION_ERROR + reason=not_found; no audit row.
- platform_operator (non-admin) delete → AUTHORIZATION_DENIED; one denial audit row; row
  untouched.

## Task 5 — exposure / completeness guard

**File:** `src/kdive/mcp/exposure.py`

- Add `"buildconfig.delete": _PLAT_ADMIN` to `_TOOL_SCOPES` (under the `# build config`
  block, beside `buildconfig.set`).
- Add `"buildconfig.list"` to `PUBLIC_TOOLS` (beside `buildconfig.get`).
- The completeness guard `tests/mcp/core/test_app.py` asserts `CLASSIFIED_TOOLS |
  PUBLIC_TOOLS == live registry`; both new tools must be present or that test fails.

**Acceptance:** `just test` (full) green, including the exposure completeness guard.

## Rollback / cleanup

Pure additive: two queries, two tool registrations, two exposure-map entries. Reverting the
branch removes them with no migration to unwind and no persisted state to clean. No object
store or DB schema change.

## Verification gaps / notes

- No CLI surface for `buildconfig.*` today, so no CLI command or CLI-registry change.
- `_docmeta.DESTRUCTIVE_TOOLS` is **not** touched (delete is `mutating()`, matching
  `shapes.delete`).
- Confirm no committed tool-manifest snapshot enumerates buildconfig tools (none found at
  plan time; re-grep before pushing).
