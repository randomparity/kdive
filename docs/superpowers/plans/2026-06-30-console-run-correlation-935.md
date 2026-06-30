# Run-correlated console artifacts + Run-scoped console manifest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for each task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correlate System-owned console artifacts (the per-Run boot-evidence snapshot and the rotating parts) to the Run active during their window via a nullable `artifacts.run_id` column, surface a bounded ordered Run-scoped console manifest on `runs.get`, and make the console surface discoverable in the `runs.get` / `artifacts.list` agent-facing wrapper docstrings.

**Architecture:** `run_id` is a **correlation** attribute orthogonal to `(owner_kind, owner_id)` ownership — console artifacts stay `owner_kind='systems'` (ADR-0273 teardown-reclaim/expiry-exclusion lifecycle unchanged). The boot worker stamps its known `run_id` exactly; the `console_rotate` worker resolves the System's most-recently-booted Run once per job under the per-System advisory lock (ordering on immutable `runs.created_at`) and stamps every part it seals. `runs.get` renders `data.console_artifacts` (newest-first total order `(created_at DESC, object_key DESC)`, bounded to `CONSOLE_MANIFEST_MAX=100` with `_total`/`_truncated`).

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`; Postgres (psycopg async), S3-compatible object store; gzip (stdlib).

## Global Constraints

- Spec: `docs/specs/2026-06-30-console-run-correlation-935.md`. ADR: `docs/adr/0279-console-run-correlation.md` (**Accepted**; cite `ADR-0279` in the module docstrings of every `src/` file changed here, per `scripts/check_adr_status.py`).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict, **whole-tree** (`just type` covers `src` + `tests`).
- Absolute imports only (no relative). Google-style docstrings on non-trivial public APIs. Functions ≤100 lines, cyclomatic ≤8, ≤5 positional params.
- Per-commit guardrails (CI gates each recipe individually): `just lint`, `just type`, then the focused tests; before the first push run the full `just ci`.
- **Backend prerequisite (before Task 1):** DB tests need disposable Postgres. Bring it up with `just compose-up`; run DB tests with `KDIVE_REQUIRE_DOCKER=1` so a missing backend **fails loudly** instead of skipping. Tasks 1, 2, 3, 4 contain such tests.
- Doc-style: plain factual prose; never "critical"/"robust"/"comprehensive"/"elegant"; "Milestone" not "Sprint".
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- `run_id` is **correlation**, never ownership: do not change any `owner_kind`/`owner_id`. Console artifacts stay `owner_kind='systems'`.
- No backfill: pre-migration rows stay `run_id = NULL`. Capture stays best-effort — a resolution failure degrades to NULL, never fails a job/boot/tool.

---

### Task 1: Migration 0054 — `artifacts.run_id` column + partial index; `Artifact` model field

**Files:**
- Add: `src/kdive/db/schema/0054_artifacts_run_id.sql`
- Modify: `src/kdive/domain/catalog/artifacts.py` (add `run_id: UUID | None = None` to `Artifact`; cite ADR-0279)
- Test: `tests/db/test_artifacts_run_id_column.py`

**Interfaces:**
- Produces: `artifacts.run_id uuid NULL REFERENCES runs (id)`; partial index `artifacts_run_id_idx ON artifacts (run_id) WHERE run_id IS NOT NULL`; `Artifact.run_id: UUID | None`.

- [ ] **Step 1: Write the failing test** — insert an `artifacts` row with a real `run_id` via `ARTIFACTS.insert(register_artifact_row(..., ))` (after Task 2 adds the kwarg, this test can set the field directly on the model) and assert the persisted row round-trips `run_id`; insert without it and assert `run_id is None`. Also assert the partial index exists (`pg_indexes`).
- [ ] **Step 2: Run test to verify it fails** — `uv run python -m pytest tests/db/test_artifacts_run_id_column.py -q` → FAIL (`Artifact` has no `run_id` / column missing). Run with `KDIVE_REQUIRE_DOCKER=1`.
- [ ] **Step 3: Implement** — write migration `0054_artifacts_run_id.sql`: `ALTER TABLE artifacts ADD COLUMN run_id uuid REFERENCES runs (id);` then `CREATE INDEX artifacts_run_id_idx ON artifacts (run_id) WHERE run_id IS NOT NULL;` (forward-only, ADR-0015). Add `run_id: UUID | None = None` to `Artifact`. The generic `Repository.insert` derives columns from `model_fields`, so no repository change is needed.
- [ ] **Step 4: Run test to verify it passes** — same command → PASS. Confirm `just type` passes (model field is `UUID | None`).
- [ ] **Step 5: Commit** — `feat(935): add nullable artifacts.run_id correlation column (mig 0054)`

**Acceptance:** column + partial index exist; existing/non-console inserts write NULL; model field type-checks.

**Rollback:** the migration is forward-only and additive; reverting the branch leaves the column unused (drop is a separate forward migration if ever needed).

---

### Task 2: `register_artifact_row` `run_id` keyword + exact boot-evidence attribution

**Files:**
- Modify: `src/kdive/artifacts/registration.py` (`register_artifact_row(..., run_id: UUID | None = None)`)
- Modify: `src/kdive/jobs/handlers/runs/boot_evidence.py` (`_upsert_console_artifact_row` passes `run_id`; cite ADR-0279)
- Test: `tests/jobs/handlers/test_boot_evidence_run_id.py` (or extend the existing boot-evidence test module)

**Interfaces:**
- Produces: `register_artifact_row(stored, *, owner_kind, owner_id, run_id=None) -> Artifact` with `run_id` populated.

- [ ] **Step 1: Write the failing test** — drive `capture_run_console` / `_upsert_console_artifact_row` for a System+Run with a fake store and assert the inserted `console-<run_id>` row has `run_id == run.id`. Add a re-capture (existing-row, changed etag) case asserting `run_id` stays that Run.
- [ ] **Step 2: Run test to verify it fails** — `uv run python -m pytest tests/jobs/handlers/test_boot_evidence_run_id.py -q` → FAIL (`register_artifact_row` has no `run_id`; row `run_id` is None).
- [ ] **Step 3: Implement** — add the `run_id` keyword to `register_artifact_row` and set it on the returned `Artifact`. In `_upsert_console_artifact_row`, pass `run_id=run_id` on the insert path. Leave the existing-row path as-is (a post-migration row already carries the correct id; a pre-migration straddle row stays NULL per the no-backfill non-goal).
- [ ] **Step 4: Run test to verify it passes** — same command → PASS.
- [ ] **Step 5: Commit** — `feat(935): stamp run_id on the per-Run boot-evidence console artifact`

**Acceptance:** the boot-evidence snapshot row carries `run_id = that Run`, exactly; every other `register_artifact_row` caller is unchanged (default `None`).

---

### Task 3: `latest_booted_run_id` resolver + rotating-part attribution

**Files:**
- Modify: `src/kdive/services/runs/steps.py` (add `latest_booted_run_id(conn, system_id) -> UUID | None`; cite ADR-0279)
- Modify: `src/kdive/jobs/handlers/console_rotate.py` (resolve once in `_rotate_under_lock`, thread `run_id` into `_seal_part` → `register_artifact_row`)
- Test: `tests/services/runs/test_latest_booted_run_id.py`, `tests/jobs/handlers/test_console_rotate_run_id.py`

**Interfaces:**
- Produces: `latest_booted_run_id(conn, system_id)` returns the most-recently-**created** Run bound to `system_id` that has a `boot` `run_steps` row, else `None`; sealed console-part rows carry that `run_id` (or NULL).

- [ ] **Step 1: Write the failing tests** —
  - resolver: two Runs on one System, both with a `boot` step; assert the later-`created_at` Run is returned. A System with no `boot` step → `None`. A `CREATED` Run with no `boot` step on the same System does not win.
  - rotation: drive `_rotate_under_lock` (fake store + seeded console file) for a System whose most-recently-booted Run is R; assert every sealed part row has `run_id == R`. A System with no resolvable boot → parts carry `run_id IS NULL` and the job still succeeds. Simulate a resolver raise → parts NULL, job does not fail (best-effort).
- [ ] **Step 2: Run tests to verify they fail** — `uv run python -m pytest tests/services/runs/test_latest_booted_run_id.py tests/jobs/handlers/test_console_rotate_run_id.py -q` → FAIL.
- [ ] **Step 3: Implement** —
  - `latest_booted_run_id`: the spec's query (`runs JOIN run_steps step='boot' WHERE system_id=%s ORDER BY r.created_at DESC LIMIT 1`). Wrap the DB call so an exception is caught by the caller (or return `None` and log once at the call site).
  - `console_rotate`: in `_rotate_under_lock`, after `_system_is_live`, resolve `run_id = await latest_booted_run_id(conn, system_id)` inside a `try/except` that logs once and falls back to `None`; pass `run_id` down through `_seal_part(..., run_id)` into `register_artifact_row(..., run_id=run_id)`.
- [ ] **Step 4: Run tests to verify they pass** — same command → PASS.
- [ ] **Step 5: Commit** — `feat(935): attribute rotating console parts to the booted Run`

**Acceptance:** parts attribute to the most-recently-booted Run resolved once under the per-System lock; NULL (never wrong) when unresolved or on resolver error; the job never fails on attribution.

---

### Task 4: `list_run_console_artifacts` + `runs.get data.console_artifacts` manifest

**Files:**
- Modify: `src/kdive/services/artifacts/listing.py` (add `list_run_console_artifacts(conn, run_id, *, limit) -> tuple[list[...], int]`; `CONSOLE_MANIFEST_MAX`; cite ADR-0279)
- Modify: `src/kdive/mcp/tools/lifecycle/runs/view.py` (call it on the success path; pass to envelope)
- Modify: `src/kdive/mcp/tools/lifecycle/runs/common.py` (render `data.console_artifacts` / `_total` / `_truncated`)
- Test: `tests/mcp/tools/lifecycle/test_runs_get_console_manifest.py`

**Interfaces:**
- Produces: `runs.get` envelope `data.console_artifacts: list[{artifact_id, object_key, created_at}]` (newest-first, `≤ CONSOLE_MANIFEST_MAX`), plus `data.console_artifacts_total` and `data.console_artifacts_truncated` when truncated; key omitted when none.

- [ ] **Step 1: Write the failing tests** —
  - a Run with a boot-evidence snapshot + 2 parts (all `run_id=run`): `runs.get` `data.console_artifacts` lists all 3, newest-first; entries carry `artifact_id`/`object_key`/`created_at`; `refs.console` unchanged; no `_truncated` key.
  - a Run with `CONSOLE_MANIFEST_MAX + 5` correlated parts: list length == `CONSOLE_MANIFEST_MAX`, `_total == MAX+5`, `_truncated is True`, the **newest** are kept.
  - total-order: two parts sharing a `created_at` are ordered by `object_key` descending, deterministically.
  - a Run with no correlated console artifacts: `data.console_artifacts` key absent.
- [ ] **Step 2: Run tests to verify they fail** — `uv run python -m pytest tests/mcp/tools/lifecycle/test_runs_get_console_manifest.py -q` → FAIL.
- [ ] **Step 3: Implement** —
  - `list_run_console_artifacts`: `SELECT id, object_key, created_at FROM artifacts WHERE run_id=%s AND owner_kind='systems' AND sensitivity='redacted' ORDER BY created_at DESC, object_key DESC LIMIT %s` with `limit = CONSOLE_MANIFEST_MAX + 1` to detect overflow, plus a cheap `count(*)`; return `(rows[:MAX], total)`.
  - `view.py`: call on the non-failed success path (where `console_ref` is computed), pass the result into `envelope_for_run`.
  - `common.py`: a `_console_manifest_data(...)` helper returns `{}` when empty, else `{console_artifacts: [...], (console_artifacts_total/_truncated when truncated)}`; merge into the success-path `data` dict next to `_console_access_data`.
- [ ] **Step 4: Run tests to verify they pass** — same command → PASS.
- [ ] **Step 5: Commit** — `feat(935): expose a Run-scoped console manifest on runs.get`

**Acceptance:** manifest content/order/bound/omission match the spec acceptance criteria; `refs.console` and `data.console_access` byte-identical to before.

---

### Task 5: Agent-facing wrapper docstrings for `runs.get` and `artifacts.list`

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/runs/registrar.py` (the `runs.get` `@app.tool` wrapper docstring)
- Modify: `src/kdive/mcp/tools/catalog/artifacts/registrar.py` (the `artifacts.list` `@app.tool` wrapper docstring)
- Regenerate: the committed tool reference via `just docs` (CI gate `just docs-check`)
- Test: extend an existing tool-surface test (or `tests/mcp/.../test_*` ) asserting the rendered `runs.get` description names `console_artifacts` and `artifacts.list` names "System-scoped" + the part-key naming. Keep assertions on substrings, not full text.

**Interfaces:**
- Produces: updated agent-facing descriptions; regenerated `docs/` tool reference snapshot.

- [ ] **Step 1: Write the failing test** — assert the live-registered `runs.get` tool description contains `console_artifacts` and the `artifacts.list` description contains `System-scoped` and `console-part`. → FAIL.
- [ ] **Step 2: Run test to verify it fails** — `uv run python -m pytest <that test> -q` → FAIL.
- [ ] **Step 3: Implement** — per spec R7: `runs.get` wrapper names `refs.console` (boot-window snapshot), `data.console_access` (how to read it), and `data.console_artifacts` (the Run-scoped manifest, bounded, newest-first, with `_total`/`_truncated`). `artifacts.list` wrapper states the listing is System-scoped (mixes every Run/session), documents `console-<run_id>` (per-Run boot snapshot) vs `console-part-<gen>-<index>` (rotating post-readiness parts), and points at `runs.get` `data.console_artifacts` for Run correlation. **Do not** put any `ADR-NNNN` token in agent-facing text (ADR-0270 guard, `tests/mcp/core/test_no_adr_leak.py`).
- [ ] **Step 4: Run test + regenerate docs** — test PASS; `just docs` (regenerate), review the diff, then `just docs-check` PASS.
- [ ] **Step 5: Commit** — `docs(935): document console refs + Run correlation in tool descriptions` (include the regenerated reference).

**Acceptance:** wrapper docstrings name the console surface; `just docs-check` and `test_no_adr_leak` pass.

---

### Task 6: Full-suite verification + ADR citation audit

- [ ] Confirm every `src/` file changed cites `ADR-0279` in its module docstring; `just adr-status-check` passes (Accepted ADR, index in sync).
- [ ] Run the **full** `just ci` (lint, type, lint-shell, lint-workflows, check-mermaid, test) green before the first push.
- [ ] Live (`live_vm`, operator-run, optional): after build/install/boot on a local-libvirt System with a post-readiness workload, `runs.get` lists the boot-evidence snapshot + rotating parts in `data.console_artifacts`, and each listed `artifact_id` reads via `artifacts.get`.
