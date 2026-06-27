# Plan: native JSON numbers/booleans in MCP tool `data` (#863)

- Spec: `docs/specs/2026-06-27-native-json-scalars-863.md`
- ADR: `docs/adr/0263-native-json-scalars-in-tool-data.md`

## Conventions for every task

- TDD: for each tool, first flip its existing test assertion(s) to the native type (the test
  goes red against current stringified output), then change the handler to make it green.
- Guardrail commands before every commit: `just lint`, `just type`, and the focused test for
  the file (`uv run python -m pytest <test path> -q`). Run `just ci` once before pushing.
- Banned doc words apply to comments/docstrings (no "robust/critical/comprehensive/…").
- Keep `Decimal` money, UUID, enum, and `transports` as strings — do not touch them.
- Where a field key maps to a `JsonValue`, the enclosing dict literal already types as
  `dict[str, JsonValue]` via `ToolResponse.success(data=...)`; for named helpers returning
  `dict[str, str]`, widen the return annotation to `dict[str, JsonValue]` and import
  `JsonValue` from `kdive.serialization`.
- Commit one logical group per task with an imperative subject ≤72 chars and the
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

## Task 1 — define the guard idiom (no file yet; authored in Task 12)

The guard test (`tests/mcp/test_no_stringified_flags.py`) is **authored last** (Task 12),
not now: an always-red test left on disk during the sweep would fail every full-suite run
(pytest collects on-disk files regardless of git state) and risks an accidental commit that
breaks `git bisect`. Task 1 only fixes the idiom the guard will enforce, so Tasks 2–11 know
the target shape.

**Guard idiom (implemented in Task 12):** AST-walk every `.py` under `src/kdive/mcp/tools/`
and fail on either: (a) a `Call` whose func is an `Attribute` `.lower` on a value that is
itself a `str(...)` call; (b) a string `Constant` exactly equal to `"true"` or `"false"`
appearing as a `Dict` value or as an `IfExp` body/orelse. Report `file:lineno`. No allowlist
— **every** such occurrence in `tools/` must be gone after the sweep, including the audit-arg
one in Task 1a below. Prose Constants whose value merely *contains* the word (e.g.
`'… content_truncated is "false".'`) are not exact matches and are not flagged.

## Task 1a — convert `inventory_export` audit `persist` flag

**Files:** `src/kdive/mcp/tools/ops/inventory_export.py`, its test (if any asserts `persist`).
**What:** `inventory_export.py:168` emits `"persist": "true"` (a bare boolean literal) inside
a `PlatformAuditEvent` `args=` mapping. Even though it is audit-only, the no-allowlist guard
flags it. Convert it to native `True` — `args: Mapping[str, object]` accepts it and only a
one-way digest is stored, so audit behavior is unchanged. This keeps the guard allowlist-free.
**Acceptance:** `persist` is `True`; any test reading it adjusted; guard would not flag it.

## Task 2 — `resources.list` capability envelope (coercion)

**Files:** `src/kdive/mcp/tools/_resource_envelopes.py`,
`tests/mcp/catalog/test_resource_envelopes.py`, `tests/mcp/catalog/test_resources_tools.py`.
**What:** Change `resource_capability_data` return type to `dict[str, JsonValue]`. For
`vcpus`/`memory_mb`/`concurrent_allocation_cap`, emit `int(value)` inside a
`try/except (TypeError, ValueError)` that drops the key on failure (preserving the existing
`is not None` drop semantics). `arch` and `transports` stay strings.
**Tests:** flip `test_resource_capability_data_flattens_known_capabilities` to expect ints
(`"vcpus": 8`, etc.); flip the `test_list_returns_host_with_flat_capability_projection`
assertions in `test_resources_tools.py` to ints. Add a coercion edge test: a resource with
`vcpus="8"` (string-stored) still yields `data["vcpus"] == 8` (int); a `vcpus="x"` drops the
key.
**Acceptance:** both tests green; `arch`/`transports` unchanged.

## Task 3 — `build_hosts.list`

**Files:** `src/kdive/mcp/tools/ops/build_hosts/lifecycle.py`,
`tests/mcp/ops/test_build_hosts.py`.
**What:** In the list item `data`, emit `max_concurrent` as `int(row["max_concurrent"])`
(DB column is integer → already int; drop `str()`), `enabled` as `bool(row["enabled"])`, and
`resolves` as the native bool from `build_host_resolves(...)` (drop `str(...).lower()`).
**Tests:** flip `["resolves"] == "true"/"false"` to `is True/False` at lines ~471-473, 501;
add/adjust an assertion that `max_concurrent` is an `int` and `enabled` is a `bool`.
**Acceptance:** test green.

## Task 4 — `artifacts.search_text` + `artifacts.get` (reads.py)

**Files:** `src/kdive/mcp/tools/catalog/artifacts/reads.py`,
`tests/mcp/catalog/test_artifacts_tools.py`, and prose sites
`src/kdive/mcp/tools/catalog/artifacts/registrar.py`,
`src/kdive/mcp/tools/lifecycle/runs/common.py`.
**What:**
- `search_text` success `data`: `match_count` → `result.match_count` (int),
  `truncated` → `result.truncated` (bool).
- `artifacts.get` windowed `data`: `size_bytes` → int, `content_truncated` → bool,
  `next_offset` → int. The two `artifact_too_large` paths (`reads.py:307`, `:356`) emit
  `size_bytes` as int.
- Update prose: `registrar.py:90` and `:102` `byte_offset` description (`content_truncated is
  "false"` → `false`), `reads.py:242` docstring, `runs/common.py:84` comment.
**Tests:** flip `["match_count"] == "1"` → `== 1`. `content_truncated` appears in
`test_artifacts_tools.py` not only in equality assertions but in **paging-loop predicates**
(e.g. `if data_str(resp, "content_truncated") == "false": break`). `data_str` is a str-typed
helper — do **not** use it for `content_truncated` post-change; read
`resp.data["content_truncated"]` (bool) directly in both the assertions and every loop
condition, or the loop compares a bool to the string `"false"` and never terminates / never
runs. Verify each paging loop still iterates and breaks correctly.
**Acceptance:** test green; `just docs` regenerates `artifacts.md` with `false` (boolean
wording); residual-prose grep clean.

## Task 5 — `artifacts.fetch_raw` + upload tools

**Files:** `src/kdive/mcp/tools/catalog/artifacts/raw_fetch.py`,
`src/kdive/mcp/tools/catalog/artifacts/uploads.py`, their tests.
**What:** `raw_fetch` success `data`: `size_bytes` → int, `ttl` → int (`asset` stays).
`uploads` `_upload_response` `data`: `expires_in` → int, `part_number` → int (when present).
**Tests:** locate the fetch_raw and upload tool tests; flip the corresponding string
assertions to int.
**Acceptance:** tests green.

## Task 6 — `catalog/shapes`

**Files:** `src/kdive/mcp/tools/catalog/shapes.py`, `tests/mcp/catalog/test_shapes_tools.py`.
**What:** `_shape_args` returns `dict[str, JsonValue]`; `vcpus`/`memory_mb`/`disk_gb` →
`int` (drop `str()`, values come from typed `SystemShape`, no coercion). `name`/`pcie_match`
stay strings. The dict still feeds `_audit_applied` (accepts `Mapping[str, object]`); widen
`_audit_applied`'s `values` param to `Mapping[str, object]` if `ty` requires.
**Tests:** flip shape `data` assertions to int.
**Acceptance:** tests green.

## Task 7 — `ops/reconcile` + `ops/reconcile_systems`

**Files:** `src/kdive/mcp/tools/ops/reconcile.py`,
`src/kdive/mcp/tools/ops/reconcile_systems.py`,
`tests/mcp/ops/test_reconcile_systems.py` and reconcile test(s).
**What:** every `str(report.<counter>)` / `str(len(diff.<bucket>))` → the native int.
**Tests:** flip the count assertions to int.
**Acceptance:** tests green.

## Task 8 — `ops/queue` + `ops/diagnostics` + `ops/resources/deregister`

**Files:** `src/kdive/mcp/tools/ops/queue.py`, `src/kdive/mcp/tools/ops/diagnostics.py`,
`src/kdive/mcp/tools/ops/resources/deregister.py`, their tests.
**What:** `queue.py`: `queue_paused` → bool (drop `"true" if … else "false"`), `depth_*` →
int. `diagnostics.py`: `has_failure`/`has_error` → bool. `deregister.py` (both return sites):
`forced` → bool.
**Tests:** flip `["has_failure"] == "true"` etc. (test_diagnostics.py:271,370-371,415-416),
queue and deregister assertions, to native bool/int.
**Acceptance:** tests green.

## Task 9 — `accounting/admin` + `accounting/reports`

**Files:** `src/kdive/mcp/tools/accounting/admin.py`,
`src/kdive/mcp/tools/accounting/reports.py`, their tests.
**What:** `admin.py` set_quota: build `values` with native ints
(`max_concurrent_allocations`, `max_concurrent_systems`, `max_pending_allocations`); the
same dict feeds `_audit_set` (widen its `values` param to `Mapping[str, object]`) and the
response `data={"project": …, **values}`. `limit_kcu` (Decimal) stays string.
`reports.py`: `project_count` → int; `reserved`/`reconciled`/`variance` (Decimal) stay
string.
**Tests:** flip quota and project_count assertions to int; assert kcu fields unchanged.
**Acceptance:** tests green.

## Task 10 — `reports/generate` + `ops/images/retention` + `ops/tuning`

**Files:** `src/kdive/mcp/tools/reports/generate.py`,
`src/kdive/mcp/tools/ops/images/retention.py`, `src/kdive/mcp/tools/ops/tuning.py`,
their tests.
**What:** `generate.py`: `count`/`section_count` → int; `truncated`/`inline_truncated` →
bool (data dict already `dict[str, JsonValue]`). `retention.py`: `pruned` → int.
`tuning.py`: response `data` `concurrent_allocation_cap` → int (the `_audit_applied` arg at
:184 stays as-is, audit-only).
**Tests:** flip the count/flag assertions.
**Acceptance:** tests green.

## Task 11 — `debug/ops` + `debug/introspect`

**Files:** `src/kdive/mcp/tools/debug/ops.py`, `src/kdive/mcp/tools/debug/introspect.py`,
`tests/mcp/debug/test_introspect_tools.py`, debug ops test.
**What:** `ops.py`: `byte_count` → int; `_stop_data` returns `dict[str, JsonValue]` with
`timed_out` → bool. `introspect.py`: the three `truncated` → bool; `script_bytes`/`max_bytes`
→ int (error `data`). Keep the `cast(ResponseData, …)` wrappers; native values satisfy them.
**Tests:** flip `["truncated"] == "false"/"true"` (test_introspect_tools.py:116,133,772) to
native bool; assert byte_count/script_bytes ints.
**Acceptance:** tests green.

## Task 12 — author guardrail test + full sweep verification

**What:** With Tasks 1a–11 done the tree has no stringified-flag idioms left. Now author
`tests/mcp/test_no_stringified_flags.py` (the idiom is defined in Task 1) and commit it.
Before committing, prove it bites: temporarily add `x = "true" if c else "false"` to a
`tools/` file and confirm the guard fails, then remove it. Run the full suite and the guard.
**Commands:** `just ci`; `uv run python -m pytest tests/mcp/test_no_stringified_flags.py -q`.
**Acceptance:** guard passes with no allowlist (incl. the converted `inventory_export`
`persist`); `just ci` green; `just docs` produces no uncommitted diff (regenerated reference
committed in Task 4).

## Rollback / cleanup

Each task is an isolated commit; reverting one restores that tool's prior string output.
No migration, no persisted state, no config touched — rollback is a pure code revert.
The only cross-task artifact is the regenerated `docs/guide/reference/artifacts.md` (Task 4)
and the guard test (authored in Task 12).

## Sequencing notes

- Tasks 1a–11 are independent (disjoint files) and may run in any order; each keeps its own
  commit green because it flips its own tests in the same commit.
- The guard test is authored only in Task 12, after the sweep, so no intermediate commit
  lands with a red guard and no always-red test sits in the working tree during Tasks 1a–11.
- `just docs` regeneration (Task 4) must be re-run and re-committed if it drifts after a base
  rebase.
