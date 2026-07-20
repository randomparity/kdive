# Plan — Upload deadline contract for agents (#1336)

Spec: [2026-07-20-upload-deadline-contract-1336.md](../specs/2026-07-20-upload-deadline-contract-1336.md)
ADR: [0394](../../adr/0394-upload-deadline-contract-fields.md)
Branch: `feat/upload-deadline-contract-1336` · Base: `main`
Guardrails: `just ci` (individually: `lint`, `type`, `lint-shell`,
`lint-workflows`, `check-mermaid`, `docs-check`, `test`). Single test:
`uv run python -m pytest tests/mcp/lifecycle/test_create_upload_tool.py -q`.

## Carried-forward review findings (from spec `/challenge`)

- **UTC normalization (F1).** Every emitted instant (`server_time`,
  `manifest_deadline`, per-item `expires_at`) MUST be
  `dt.astimezone(UTC).isoformat()` — never bare `.isoformat()`. Postgres
  `now()` is `timestamptz` rendered in the session TZ, which is not guaranteed
  UTC. Tests assert the string ends with `+00:00`.
- **Which deadline binds (F2).** The docstrings must state the per-URL
  `expires_at` is the deadline for *starting that PUT* and can be earlier than
  the collection `manifest_deadline` (reaper reclaim of the whole upload),
  because `presign_ttl = min(3600, UPLOAD_TTL)` clamps below the manifest ttl
  when `UPLOAD_TTL > 3600`. A test covers the `UPLOAD_TTL > 3600` divergence.

## TDD ordering

Each task writes the failing test first, then the change to green it. Run the
single-file test after each; run `just ci` before the final commit.

---

### Task 1 — `replace_manifest` returns a `(server_time, deadline)` stamp

**Fits:** the clock and the reaper's deadline must come from one Postgres
transaction so they cannot disagree (ADR-0394 decision 3).

**Files:** `src/kdive/artifacts/upload_manifest.py`,
`tests/db/test_upload_manifest.py`.

**Change:**
- Add a `NamedTuple ManifestStamp(server_time: datetime, deadline: datetime)`.
- Change `replace_manifest` to `... RETURNING now(), deadline`, fetch the row,
  and return `ManifestStamp(server_time=row_now, deadline=row_deadline)`. Use a
  cursor so the `RETURNING` row is fetchable.
- Both values are tz-aware (`timestamptz`). Do **not** normalize here — return
  raw aware datetimes; UTC normalization happens at the response-render seam
  (keeps this a storage function).

**Acceptance:**
- New/updated test in `test_upload_manifest.py`: after `replace_manifest` with a
  known ttl, `stamp.deadline - stamp.server_time == ttl` (exact), and
  `stamp.deadline` equals the `deadline` a subsequent `get_manifest` reads.
- Both datetimes are timezone-aware.

**Rollback:** revert the signature; sole caller is Task 2.

---

### Task 2 — thread the stamp into the upload responses; render the new fields

**Fits:** the response-render seam (`_create_upload` → `_upload_response` /
`ToolResponse.collection`) where the agent-facing fields are built.

**Files:** `src/kdive/mcp/tools/catalog/artifacts/uploads.py`,
`tests/mcp/lifecycle/test_create_upload_tool.py`.

**Change:**
- Import `UTC` (from `datetime`).
- Add a small helper `def _iso_utc(dt: datetime) -> str: return
  dt.astimezone(UTC).isoformat()` (F1 — never bare `.isoformat()`).
- In `_create_upload`, capture `stamp = await upload_manifest.replace_manifest(...)`.
- After the transaction, compute:
  - `server_time = _iso_utc(stamp.server_time)`
  - `manifest_deadline = _iso_utc(stamp.deadline)`
  - per-item `expires_at = _iso_utc(stamp.server_time + timedelta(seconds=_presign_ttl_seconds()))`
- Extend `_upload_response(upload, *, next_action, expires_at)` to add
  `"expires_at": expires_at` to item `data` (keep `expires_in`).
- Extend the collection `data` with `server_time`, `manifest_deadline`, and
  `on_expiry = {"tool": _upload_tool_name(spec), "effect": "re-mint replaces the
  manifest and resets the deadline"}`. Keep `manifest_mode` /
  `replaces_prior_manifest`. `suggested_next_actions` unchanged (`[next_action]`).

**Acceptance (AC1–AC5):**
- `create_run_upload` collection `data` has `server_time`, `manifest_deadline`,
  `on_expiry == {"tool": "artifacts.create_run_upload", "effect": ...}`; each
  item `data` has `expires_at` and `expires_in`.
- `create_system_upload` same, `on_expiry.tool ==
  "artifacts.create_system_upload"`.
- All three instants parse as ISO-8601 and end with `+00:00` (F1).
- `manifest_deadline` parsed − `server_time` parsed == `UPLOAD_TTL_SECONDS`;
  item `expires_at` − `server_time` == `_presign_ttl_seconds()`.
- Chunked upload: one `expires_at` per part item, one collection-level
  `server_time`/`manifest_deadline`/`on_expiry` (AC4).
- Unchanged: `expires_in`, `manifest_mode`, `replaces_prior_manifest`,
  `required_headers`, audit row, atomic rollback, rejection paths (AC5). First
  grep the test file for any exact-`data`-dict equality assertion that additive
  keys would break (none expected — existing asserts use key access).

**Divergence test (F2):** with `UPLOAD_TTL_SECONDS` monkeypatched > 3600 (e.g.
7200), assert item `expires_at` − `server_time` == 3600 while `manifest_deadline`
− `server_time` == 7200 (the per-URL wall is earlier than the manifest window).

**Rollback:** revert `uploads.py`; Task 1 stands alone.

---

### Task 3 — state scope, recovery, and the non-constraint in the wrapper docstrings

**Fits:** the agent-facing contract (AGENTS.md: the `@app.tool` wrapper docstring
is what serializes into the tool schema / generated reference).

**Files:** `src/kdive/mcp/tools/catalog/artifacts/registrar.py`, a
description-assertion test (add to `tests/mcp/lifecycle/test_create_upload_tool.py`
or the existing schema test), `docs/guide/reference/artifacts.md` (regenerated).

**Change:** extend both `artifacts_create_run_upload` and
`artifacts_create_system_upload` wrapper docstrings with plain, factual prose:
- Scope (F2): "Start each PUT before that upload item's `data.expires_at`; an
  in-flight transfer already begun is not interrupted at expiry. `expires_at` can
  be earlier than `data.manifest_deadline`."
- `data.manifest_deadline` is the reaper-enforced deadline for the whole upload
  if it is not finalized; `data.server_time` is the reference clock to measure
  it against.
- Recovery: re-calling this tool (`manifest_mode: "replace"`) resets the
  deadline; see `data.on_expiry`.
- Non-constraint: `chunks` are for objects larger than the 5 GiB single-PUT size
  limit, not for time pressure.
- Keep the existing `required_headers` / `403 SignatureDoesNotMatch` /
  redeclare-every-artifact sentences.
- Observe doc-style: no "critical/robust/comprehensive"; use plain prose.

**Acceptance (AC6):**
- A test asserts the wrapper docstring (or the FastMCP tool description) for both
  tools contains the scope sentence and the "not for time pressure" clause.
- Run `just docs`; `docs/guide/reference/artifacts.md` reflects the new prose;
  `just docs-check` passes.

**Rollback:** revert the docstrings and re-run `just docs`.

---

### Task 4 — finalize: guardrails + docs regen

- Run `just docs` (regenerate the tool reference) and `just rbac-matrix` only if
  affected (it is not — no RBAC change).
- Run `just ci`. Fix any `lint`/`type`/`docs-check` fallout.
- Commit tasks as logically-scoped commits (Task 1 storage; Task 2 responses;
  Task 3 docstrings + regen). Keep history bisectable.

## Verification summary

- `AC1`–`AC5`: Task 2 tests.
- `AC3` manifest-deadline == reaper's value: Task 1 test.
- `AC4` chunked: Task 2 test (extends existing `test_chunked_artifact_*`).
- `AC6` descriptions + regen: Task 3.
- `AC7`: Task 4 `just ci`.

## Out of scope / non-goals

Reaper behavior, TTL config values, manifest schema, `refresh_deadline`, the
presign mechanism, and promoting `server_time` to the shared envelope (deferred
to the third deadline-bearing surface per ADR-0394).
