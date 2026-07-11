# Plan — Actionable retry against a failed System on `systems.provision` (#512)

- **Spec:** [docs/specs/2026-06-17-failed-system-provision-retry-ergonomics.md](../../specs/2026-06-17-failed-system-provision-retry-ergonomics.md)
- **ADR:** [0149](../../adr/0149-failed-system-provision-retry-ergonomics.md)
- **Issue:** [#512](https://github.com/randomparity/kdive/issues/512)

Tightly-coupled change across two source files + their tests + a docstring; implemented
directly in one session with TDD (not subagent fan-out). Each step: failing test first, then
minimal implementation, then guardrails.

## Guardrail commands (run before every commit)

- `just lint` — ruff check + format check
- `just type` — ty over src **and** tests
- focused: `uv run python -m pytest <file>::<test> -q`
- before first push: `just lint && just type && just test` plus `just docs-check`,
  `just config-docs-check`, `just config-guard`, `just adr-status-check`, `just docs-links`.
  (`just check-mermaid` is broken on clean `origin/main` — pre-existing node tooling error,
  unrelated to this change; note in PR.)

## Step 1 — `queue.get_by_dedup_key`

**Where:** `src/kdive/jobs/queue.py`. **Files touched:** `queue.py`, a queue test.

A connection-scoped read returning the `Job` for a `dedup_key` (the unique key,
`jobs_dedup_key_key`, `schema/0001_init.sql:169`) or `None`. Mirrors the existing
`SELECT * FROM jobs WHERE dedup_key = %s` already used inside `enqueue` (queue.py:73), but as a
standalone read with no transaction/insert.

```python
async def get_by_dedup_key(conn: AsyncConnection, dedup_key: str) -> Job | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM jobs WHERE dedup_key = %s", (dedup_key,))
        row = await cur.fetchone()
    return None if row is None else Job.model_validate(row)
```

**TDD:** in the relevant queue test module (find with
`rg -l "queue.enqueue|def test_.*queue" tests/`), add:
- enqueue a job, `get_by_dedup_key` returns the same row (id matches);
- `get_by_dedup_key` on an unknown key returns `None`.

**Acceptance:** both tests pass; `just type` clean (return type `Job | None`).

## Step 2 — failed-System branch in admission

**Where:** `src/kdive/services/systems/admission.py` `_provision_create_response`
(the catch-all at ~line 418). **Files touched:** `admission.py`, `tests/mcp/lifecycle/test_systems_tools.py`.

Add, **before** the catch-all `return _failure(existing.id, …)`:

```python
if existing.state is SystemState.FAILED:
    return await _failed_system_retry_failure(conn, alloc, existing)
return _failure(
    existing.id,
    data={"current_status": existing.state.value},
    suggested_next_actions=("allocations.release", "allocations.request"),
)
```

New helper `_failed_system_retry_failure(conn, alloc, existing)`:
- builds the fixed actionable sentence (System is `failed`; release + re-request the
  allocation for a fresh System);
- `job = await queue.get_by_dedup_key(conn, f"{alloc.id}:provision")`;
- when the job has a `failure_message`, append it to the sentence as the surfaced reason;
- `data` = `{"current_status": "failed"}` + (`failing_job_id` + any `failure_detail_*` keys)
  when the job is present;
- returns `_failure(existing.id, CONFIGURATION_ERROR, data=…, detail=sentence,
  suggested_next_actions=("allocations.release", "allocations.request"))`.

Reuse the failure-context extraction shape from
`mcp/tools/lifecycle/runs/common.py:_failed_envelope` (lines 64-74): `failure_message` →
detail, `failure_detail_*` → data. No new redaction (worker already redacted; ADR-0141/0149).

**Note on no-leak:** the branch only emits `CONFIGURATION_ERROR` (non-suppressed), and the
surfaced data is the worker-redacted context, so the `data`-extras-bypass-suppression concern
(common.py:55-63) does not apply here — but keep the surface limited to the worker-redacted
`failure_*` keys, never `str(exc)`.

**TDD (in `tests/mcp/lifecycle/test_systems_tools.py`, behaviour via the provision handler):**
1. Seed a granted→active allocation + a `failed` System for it, enqueue+fail a provision job
   (dedup `f"{alloc}:provision"`) carrying a `failure_context={"failure_message": "<reason>"}`;
   retry `provision_system` → `error_category == "configuration_error"`,
   `detail` contains both the fixed sentence and `<reason>`,
   `data["current_status"] == "failed"`, `data["failing_job_id"]` set,
   `suggested_next_actions == ["allocations.release", "allocations.request"]`,
   and the returned `object_id` equals the seeded System id (no re-mint).
2. Idempotency: two successive retries return identical `detail` / actions / `object_id`;
   assert exactly one System row for the allocation.
3. No provision job row: seed `failed` System without the job; retry → `detail` is the fixed
   sentence alone (non-empty), no `failing_job_id` key.
4. Non-`failed` terminal (`torn_down`): retry → `configuration_error`,
   `current_status == "torn_down"`, actions == release/request, no job reason.
5. `failure_detail_*` key present in the job context is copied verbatim into `data`.

Use the existing `_seed_system`, `_granted_allocation`/active-allocation helpers, and the
`queue.enqueue` + `queue.fail` (or direct row write) patterns already in the test module.

**Acceptance:** all 5 tests pass; `just lint`, `just type` clean; function ≤100 lines,
complexity ≤8, ≤5 positional params (helper takes conn+alloc+existing = 3).

## Step 3 — document Allocation↔System cardinality

**Where:** `provision_system` docstring in
`src/kdive/mcp/tools/lifecycle/systems/provision.py`. **Files touched:** `provision.py`,
regenerated `docs/` tool reference.

Extend the one-line docstring to state: one System per Allocation; a retry against a `failed`
System does not mint a new one — release and re-request the Allocation for a fresh System.

**Then regenerate the committed tool reference:** `just docs` (mutating), review the diff,
`just docs-check` to confirm in sync. (Cross-agent conflict zone — keep additive.)

**Acceptance:** `just docs-check` passes; docstring change is reflected in the generated ref.

## Step 4 — full suite + guardrails before push

Run `just lint && just type && just test`, then `just docs-check`,
`just config-docs-check`, `just config-guard`, `just adr-status-check`, `just docs-links`,
`just lint-shell`, `just lint-workflows`. Fix every warning. Fold fixups into the logical
commit before the first push.

## Rollback / cleanup

Pure additive behaviour change on a failure branch + one new read helper; no migration, no
schema, no state mutation added. Rollback = revert the branch. The new `get_by_dedup_key` is a
read with no side effects.
