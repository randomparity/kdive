# Per-Run vmcore capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task (the core change is tightly coupled — execute inline, not via parallel
> subagents). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make raw vmcore capture Run-addressed (`vmcore.fetch(run_id, method)`) and cores
Run-owned (`owner_kind='runs'`), so the core is attributed to the crashing Run and the #781 egress
resolves it by `run_id` directly.

**Architecture:** Thread `run_id` from the `vmcore.fetch` tool → `CaptureVmcorePayload` → the
`Retriever.capture` port → artifact owner/key in all three providers. `raw_vmcore_key` flips from
System-keyed to Run-keyed; the capture handler's dedup lock moves from `LockScope.SYSTEM` to
`LockScope.RUN`. The egress + postmortem/introspect readers resolve the core by `run_id`.

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`; psycopg async; FastMCP; Postgres
advisory locks; S3-compatible object store.

## Global Constraints

- Source of truth for checks: the `justfile`. Before every commit run `just lint` (`ruff check` +
  `ruff format --check`), `just type` (`ty check`, **whole tree**), and the focused tests; run the
  full `just test` (`-m "not live_vm and not live_stack"`) before the first push.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. Absolute imports only. ≤100 lines/function,
  cyclomatic ≤8.
- `ty` is strict, whole-tree (src + tests). A type error in a caller you did not edit still fails —
  the core switch must update every caller in the same commit.
- Pick the most specific existing `ErrorCategory`; never invent strings. Every tool returns a
  `ToolResponse`; an `error_category` is set iff status is a failure.
- Doc prose guard: use "Milestone" not "Sprint"; avoid "critical/robust/comprehensive/elegant".
- Commit trailer (required): `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- ADR cite: source modules that change behavior cite `ADR-0244` in their docstring.

---

## File Structure

- `src/kdive/jobs/payloads.py` — `CaptureVmcorePayload` becomes a `RunPayload` (`run_id` + `method`);
  `CAPTURE_VMCORE` registered run-bearing.
- `src/kdive/db/artifact_queries.py` — `raw_vmcore_key(conn, run_id)` queries `owner_kind='runs'`.
- `src/kdive/providers/ports/retrieve.py` — `Retriever.capture(system_id, run_id, method)`.
- `src/kdive/providers/local_libvirt/retrieve.py`, `src/kdive/providers/fault_inject/retrieve.py`,
  `src/kdive/providers/remote_libvirt/retrieve/{facade,common,kdump_capture,host_dump_capture}.py` —
  accept `run_id`; write `owner_kind='runs'`, `owner_id=str(run_id)`.
- `src/kdive/jobs/handlers/vmcore.py` — Run-addressed precheck/finalize, `LockScope.RUN`, audit
  `object_kind='runs'`.
- `src/kdive/mcp/tools/lifecycle/vmcore.py` — `vmcore.fetch(run_id, method)` admission.
- `src/kdive/mcp/tools/_vmcore_targets.py` — `raw_vmcore_key(conn, uid)` (per-Run).
- `src/kdive/mcp/tools/catalog/artifacts/raw_fetch.py` — `vmcore` branch resolves by `run_id`, gates
  on `run.project`.
- `src/kdive/services/artifacts/listing.py` — add `list_redacted_run_artifacts` (`owner_kind='runs'`).
- `src/kdive/mcp/tools/lifecycle/vmcore.py` — `vmcore.list(run_id)` lists the Run's redacted cores.
- `docs/guide/reference/vmcore.md` — regenerated (covers both `vmcore.fetch` and `vmcore.list`).

---

## Task 1: The Run-addressing switch (coupled core change)

This is one green commit: `raw_vmcore_key`'s meaning flips and every caller — the write path
(handler/providers/port/payload/tool) and the read path (`_vmcore_targets`, `raw_fetch`) — must move
together or `ty`/tests go red. Work the sub-steps in order; only run guardrails + commit at the end.

**Files:**
- Modify: `src/kdive/jobs/payloads.py`, `src/kdive/db/artifact_queries.py`,
  `src/kdive/providers/ports/retrieve.py`,
  `src/kdive/providers/local_libvirt/retrieve.py`, `src/kdive/providers/fault_inject/retrieve.py`,
  `src/kdive/providers/remote_libvirt/retrieve/{facade,common,kdump_capture,host_dump_capture}.py`,
  `src/kdive/jobs/handlers/vmcore.py`, `src/kdive/mcp/tools/lifecycle/vmcore.py`,
  `src/kdive/mcp/tools/_vmcore_targets.py`, `src/kdive/mcp/tools/catalog/artifacts/raw_fetch.py`
- Test: `tests/db/test_artifact_queries.py`, `tests/jobs/test_payloads.py`,
  `tests/mcp/lifecycle/test_vmcore_tools.py`, `tests/mcp/test_vmcore_targets.py`,
  the three providers' retrieve tests, `tests/mcp/catalog/artifacts/` fetch_raw tests.

**Interfaces produced (names later tasks/tests rely on):**
- `CaptureVmcorePayload(run_id: str, method: CaptureMethod)` — `RunPayload` subclass.
- `raw_vmcore_key(conn: AsyncConnection, run_id: UUID) -> str | None` — `owner_kind='runs'`.
- `Retriever.capture(system_id: UUID, run_id: UUID, method: CaptureMethod) -> CaptureOutput`.
- `vmcore.fetch(run_id: str, method: CaptureMethod | None, idempotency_key: str | None)`.
- Object key `{tenant}/runs/{run_id}/vmcore-{method}` (+ `-redacted`).

- [ ] **Step 1.1 — Payload (test first).** In `tests/jobs/test_payloads.py`, change the
  `CAPTURE_VMCORE` payload tests to use `{"run_id": <uuid>, "method": "host_dump"}` and assert
  `run_id_from_payload(JobKind.CAPTURE_VMCORE, {"run_id": str(rid), "method": "kdump"}) == rid`.
  Run `uv run python -m pytest tests/jobs/test_payloads.py -q` → expect FAIL.

- [ ] **Step 1.2 — Payload (implement).** In `src/kdive/jobs/payloads.py`: make
  `CaptureVmcorePayload(RunPayload)` with field `method: CaptureMethod` (drop the `SystemPayload`
  base, so no `system_id`). Add `JobKind.CAPTURE_VMCORE: CaptureVmcorePayload` to
  `_RUN_PAYLOAD_MODELS`. Keep `CaptureVmcorePayload` in the `_PayloadModel`/`PayloadModel` unions and
  the `_PAYLOAD_MODELS` map. Run the test → PASS.

- [ ] **Step 1.3 — `raw_vmcore_key` per-Run (test first).** In `tests/db/test_artifact_queries.py`,
  rewrite `test_raw_vmcore_key_*` to insert an `owner_kind='runs'` artifact with key
  `.../runs/{run_id}/vmcore-host_dump` (+ a `-redacted` sibling) and assert
  `raw_vmcore_key(conn, run_id)` returns the raw key, the redacted is excluded, and a random
  `run_id` returns `None`. Run that test file → expect FAIL.

- [ ] **Step 1.4 — `raw_vmcore_key` per-Run (implement).** In `src/kdive/db/artifact_queries.py`,
  change `_RAW_VMCORE_KEY_SQL` to `owner_kind = 'runs'` and rename the parameter:
  ```python
  async def raw_vmcore_key(conn: AsyncConnection, run_id: UUID) -> str | None:
      """Return the Run's raw ``vmcore-{method}`` object key, or ``None`` (ADR-0244)."""
      async with conn.cursor(row_factory=dict_row) as cur:
          await cur.execute(_RAW_VMCORE_KEY_SQL, (run_id, _RAW_VMCORE_KEY_LIKE, _REDACTED_VMCORE_LIKE))
          row = await cur.fetchone()
      return None if row is None else str(row["object_key"])
  ```
  with `_RAW_VMCORE_KEY_SQL = ("SELECT object_key FROM artifacts WHERE owner_kind = 'runs' AND
  owner_id = %s AND object_key LIKE %s AND object_key NOT LIKE %s")`. Run the db test → PASS.

- [ ] **Step 1.5 — Capture port + providers (implement).** In
  `src/kdive/providers/ports/retrieve.py` change `Retriever.capture` to
  `def capture(self, system_id: UUID, run_id: UUID, method: CaptureMethod) -> CaptureOutput`.
  In each provider, accept `run_id` and pass `owner_kind="runs", owner_id=str(run_id)` to every
  `ArtifactWriteRequest`/`ArtifactStreamRequest`/`artifact_key(...)`/`register`:
  - `local_libvirt/retrieve.py`: `capture(system_id, run_id, method)` →
    `_capture_via_file(system_id, run_id, method, core)`; `_put_stream`/`_put` take `run_id` and set
    `owner_kind="runs", owner_id=str(run_id)`. The `system_id` stays in error `details` and seam
    calls (`_wait_for_vmcore(system_id)`, `_host_dump_capture(system_id)`).
  - `fault_inject/retrieve.py`: `capture(system_id, run_id, method)`; `_put(run_id, name, sens)` →
    `owner_kind="runs", owner_id=str(run_id)`.
  - `remote_libvirt/retrieve/facade.py`: `capture(system_id, run_id, method)` →
    `self._host_dump.capture(system_id, run_id)` / `self._kdump.capture(system_id, run_id)`.
  - `remote_libvirt/retrieve/kdump_capture.py`: `capture(system_id, run_id)`;
    `artifact_key(TENANT, "runs", str(run_id), f"vmcore-{method.value}")`; `_reference(...)`
    owner stays via the key; `persist_redacted(... run_id ...)`.
  - `remote_libvirt/retrieve/host_dump_capture.py`: `capture(system_id, run_id)`; `_store_core`,
    `_stream_put` use `owner_kind=OWNER_KIND_RUNS, owner_id=str(run_id)`.
  - `remote_libvirt/retrieve/common.py`: add `OWNER_KIND_RUNS = "runs"`; in `persist_redacted`
    **replace** the `system_id: UUID` parameter with `run_id: UUID` (it is used only for `owner_id`)
    and set `owner_kind=OWNER_KIND_RUNS, owner_id=str(run_id)`, key name unchanged — so the redacted
    sibling co-owns with the raw core. Keep the volume/domain names keyed on `system_id`.
  Update the three providers' retrieve tests to call `capture(system_id, run_id, method)` and assert
  the stored key is `.../runs/{run_id}/vmcore-{method}`.

- [ ] **Step 1.6 — Worker handler (implement).** In `src/kdive/jobs/handlers/vmcore.py`. First add
  the imports this step needs: `RUNS` to the existing `from kdive.db.repositories import ARTIFACTS,
  SYSTEMS` line, and `Run` to the existing `from kdive.domain.lifecycle import System`.
  - `captured_method`/`ensure_method_match` unchanged (still parse `/vmcore-`).
  - `precheck` is now Run-addressed under `LockScope.RUN`:
    ```python
    async def precheck_run(conn, run_id, method) -> tuple[Run, System] | str:
        async with conn.transaction(), advisory_xact_lock(conn, LockScope.RUN, run_id):
            run = await RUNS.get(conn, run_id)
            if run is None or run.system_id is None:
                raise CategorizedError("capture target run is gone or unbound",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE, details={"run_id": str(run_id)})
            system = await SYSTEMS.get(conn, run.system_id)
            if system is None:
                raise CategorizedError("capture target system is gone",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE, details={"system_id": str(run.system_id)})
            existing = await raw_vmcore_key(conn, run_id)
            if existing is not None:
                ensure_method_match(existing, method, run_id)  # details key -> run_id
                return existing
            return run, system
    ```
  - `finalize_capture(conn, job, run, method, output)` acquires `LockScope.RUN, run.id`, re-checks
    `raw_vmcore_key(conn, run.id)`, inserts both rows with
    `register_artifact_row(output.raw, owner_kind="runs", owner_id=run.id)` (and `.redacted`), and
    audits `object_kind="runs", object_id=run.id, args={"run_id": str(run.id)}` with `run.project`.
  - `capture_handler`: `payload = load_payload(job, CaptureVmcorePayload)`;
    `run_id = UUID(payload.run_id)`; `precheck = await precheck_run(conn, run_id, method)`; if str
    return it; `(run, system) = precheck`; `binding = await resolver.binding_for_system(conn,
    system.id)`; `output = await asyncio.to_thread(retriever.capture, system.id, run.id, method)`;
    `result = await finalize_capture(conn, job, run, method, output)`.
  - `ensure_method_match` signature param renames `system_id` → `run_id` (details key `run_id`).

- [ ] **Step 1.7 — `vmcore.fetch` admission (implement + test).** In
  `src/kdive/mcp/tools/lifecycle/vmcore.py`: `fetch_vmcore`/`_fetch_vmcore` take `run_id` instead of
  `system_id`. Resolve `run = await RUNS.get(conn, uid)`; `not_found`/`config_error` for
  absent/cross-project/malformed; `system_id = run.system_id` — if `None`, `config_error`
  ("run is not bound to a system"); `system = await SYSTEMS.get(conn, system_id)`; require
  `CRASHED`; `require_role(ctx, run.project, CONTRIBUTOR)`; resolve method against the bound
  provider's descriptor (use `with_runtime_for_run`); enqueue
  `CaptureVmcorePayload(run_id=run_id, method=capture_method)` with dedup
  `f"{run_id}:capture_vmcore:{capture_method.value}"`; envelope object id is `run_id`. Update the
  `@app.tool` `vmcore_fetch` signature: first arg `run_id` (description "The crashed Run whose
  vmcore to capture."). Use `with_runtime_for_run` (not `_for_system`) so the runtime resolves from
  the Run's System. Update `tests/mcp/lifecycle/test_vmcore_tools.py` accordingly (admission
  happy-path + each rejection in Task-1 failure list).

- [ ] **Step 1.8 — `_vmcore_targets` + `raw_fetch` (implement).** In
  `src/kdive/mcp/tools/_vmcore_targets.py`: `resolve_run_vmcore_target` calls
  `raw_vmcore_key(conn, uid)` (the Run id) instead of `raw_vmcore_key(conn, run.require_system_id())`.
  In `src/kdive/mcp/tools/catalog/artifacts/raw_fetch.py` `_resolve_key`, the `vmcore` branch:
  ```python
  require_role(ctx, run.project, Role.CONTRIBUTOR)
  key = await raw_vmcore_key(conn, uid)   # uid = the Run UUID
  if key is None:
      return _config_error(run_id, data={"reason": "vmcore_unavailable"})
  return key
  ```
  Drop the `run.system_id`/`system_project` use in this branch (and remove the now-unused
  `system_project` import here if no longer referenced in this file). Thread the Run `uid` into
  `_resolve_key` (it currently takes `run_id: str`; pass the parsed `uid`). Update the fetch_raw
  egress tests to seed an `owner_kind='runs'` core.

- [ ] **Step 1.9 — `vmcore.list(run_id)` Run-addressed (test + implement).** Moving the redacted
  sibling to `owner_kind='runs'` makes the System-scoped `list_redacted_system_artifacts` return no
  vmcores, so `vmcore.list` must address the Run. In `src/kdive/services/artifacts/listing.py` add
  `list_redacted_run_artifacts(pool, ctx, *, run_id: str) -> list[RedactedArtifact]`, a mirror of
  `list_redacted_system_artifacts` that resolves the **Run's** project (reject absent/cross-project
  with empty list, then `require_role(ctx, run.project, Role.VIEWER)`) and runs
  `SELECT id, object_key FROM artifacts WHERE owner_kind='runs' AND owner_id=%s AND sensitivity=%s`
  with `Sensitivity.REDACTED.value`. In `src/kdive/mcp/tools/lifecycle/vmcore.py`: `list_vmcores`
  takes `run_id`, calls `list_redacted_run_artifacts(...)`, keeps the `_is_redacted_vmcore` filter;
  the `@app.tool` `vmcore_list` first arg becomes `run_id` (description "The Run whose redacted
  vmcore artifacts to list."). Update `tests/mcp/lifecycle/test_vmcore_tools.py`: seed a Run-owned
  redacted vmcore and assert `vmcore.list(run_id)` surfaces it (this is the test that would have
  caught the silent-empty regression). Run the vmcore tools test module → PASS.

- [ ] **Step 1.10 — `RunFetchContext.system_id` dead-code check.** After Step 1.8, grep
  `rg -n "\.system_id" src/kdive/mcp/tools/catalog/artifacts/raw_fetch.py` and
  `rg -n "system_id" src/kdive/db/artifact_queries.py`. If `RunFetchContext.system_id` is unused
  across `src/` and `tests/` (it likely remains used by the `vmlinux`/not-found path — verify), keep
  it; if fully unused, remove the field, its SQL column, and its docstring line (no dead code).
  Record the decision in the commit message.

- [ ] **Step 1.11 — Guardrails + commit.** `just lint && just type` then the focused suites:
  `uv run python -m pytest tests/jobs/test_payloads.py tests/db/test_artifact_queries.py
  tests/mcp/lifecycle/test_vmcore_tools.py tests/mcp/test_vmcore_targets.py
  tests/services/artifacts tests/providers -m "not live_vm and not live_stack" -q` and the fetch_raw
  test module. All green.
  ```bash
  git add -A
  git commit -m "feat(vmcore): Run-addressed capture, Run-owned cores (#796)

  vmcore.fetch(run_id, method); cores keyed owner_kind='runs'; raw_vmcore_key
  resolves per-Run; capture dedup/lock move to LockScope.RUN; #781 egress
  resolves the core by run_id. ADR-0244.

  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
  ```

---

## Task 2: Regenerate the agent-facing tool reference

**Files:** Modify (generated): `docs/guide/reference/vmcore.md`.

- [ ] **Step 2.1 — Regenerate.** Run `just docs` (i.e.
  `uv run python scripts/gen_tool_reference.py`).
- [ ] **Step 2.2 — Verify the diff** shows only the `vmcore.fetch` argument change
  (`system_id` → `run_id`) and any wording derived from the new docstring; no unrelated churn. Then
  run `just docs-check` to confirm the committed reference matches a fresh generation (the CI gate).
- [ ] **Step 2.3 — Commit.**
  ```bash
  git add docs/guide/reference/vmcore.md
  git commit -m "docs(vmcore): regenerate tool reference for run_id argument (#796)

  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
  ```

---

## Task 3: Adversarial same-Run concurrency test

**Files:** Create/extend: `tests/adversarial/test_vmcore_capture_idempotency.py` (or extend the
existing adversarial capture test if one exists — grep `tests/adversarial` first).

**Interfaces consumed:** `raw_vmcore_key(conn, run_id)`, the `capture_handler`, `precheck_run`,
`finalize_capture`, `CaptureVmcorePayload`.

- [ ] **Step 3.1 — Write the concurrency test.** Drive two concurrent
  `capture_handler` invocations for the **same** `run_id` + method against a disposable Postgres
  (testcontainers, the `migrated_url` fixture pattern used in `tests/db`), with a fake retriever that
  writes a deterministic `owner_kind='runs'` core. Assert exactly **one** raw `owner_kind='runs'`
  artifact row exists for that `run_id` afterward (the per-Run lock + `finalize` re-check serialize
  the race). Also assert two **distinct** `run_id`s each capturing yield two distinct rows. Follow
  the existing adversarial-suite structure (hypothesis/async harness) — mirror the nearest existing
  capture/idempotency adversarial test.
- [ ] **Step 3.2 — Run it** (`uv run python -m pytest
  tests/adversarial/test_vmcore_capture_idempotency.py -q`) → PASS; break the `finalize` re-check
  locally to confirm the test catches a double-insert, then restore.
- [ ] **Step 3.3 — Commit.**
  ```bash
  git add tests/adversarial/test_vmcore_capture_idempotency.py
  git commit -m "test(vmcore): per-Run capture idempotency under concurrency (#796)

  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
  ```

---

## Task 4: Cross-project egress denial test

**Files:** Extend the `artifacts.fetch_raw` test module (the one touched in Step 1.8).

- [ ] **Step 4.1 — Write the test.** Seed a Run in project A bound to a System in project A with a
  Run-owned `vmcore` core, and a caller holding only project B. Assert `fetch_raw(run_id, "vmcore")`
  returns the existence-masking `not_found` envelope (no project-A leak). Add a member-but-below-
  `contributor` case asserting the audited denial. This guards the `run.project == system.project`
  invariant the egress gate move relies on.
- [ ] **Step 4.2 — Run it** → PASS.
- [ ] **Step 4.3 — Commit.**
  ```bash
  git add tests/mcp/catalog/artifacts/
  git commit -m "test(vmcore): fetch_raw vmcore cross-project denial (#796)

  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
  ```

---

## Rollback / cleanup

Pure code + docs; no migration. To roll back, revert the four commits; no persisted production
state exists at per-System or per-Run keys to unwind (M0/M1 carries no production cores).

## Self-review notes

- Spec coverage: AC#1 → Task 1 (db/providers) + Task 3 (distinct rows); AC#2 → Step 1.7 + tests;
  AC#3 → Step 1.6 `ensure_method_match` + handler test; AC#4 → Task 3; AC#5 → unchanged
  (`keyed_mutation`, exercised by existing idempotency test, re-verified in Step 1.7); AC#6 → Step
  1.8; AC#7 → Step 1.8 (`_vmcore_targets`, shared by `introspect.from_vmcore`) + Step 1.9
  (`vmcore.list`); AC#8 → Task 2 (covers both `vmcore.fetch` and `vmcore.list`).
- Cross-project denial (spec design note) → Task 4.
- `vmcore.list` redacted-sibling regression (forced by the `owner_kind='runs'` move) → Step 1.9.
- Type consistency: `raw_vmcore_key(conn, run_id)`, `capture(system_id, run_id, method)`,
  `CaptureVmcorePayload(run_id, method)` used identically in every referencing step.
