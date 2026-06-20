# Plan: Uniform `idempotency_key` on mutations (#619)

Spec: [`../../design/uniform-mutation-idempotency.md`](../../design/uniform-mutation-idempotency.md)
· ADR: [`../../adr/0193-uniform-mutation-idempotency.md`](../../adr/0193-uniform-mutation-idempotency.md)
· Issue: #619 (part of #618, AX_REVIEW A1)

## Context for every task

- Repo: `/home/dave/src/kdive-worktrees/feat-idempotency-key-619`, branch
  `feat/idempotency-key-619`. **Work in this worktree only** (outside the main repo tree).
- Conventions: Python 3.14, `uv`; `ruff` lint/format, `ty` types, `pytest`. Absolute imports
  only; ≤100 lines/function; Google-style docstrings on public APIs; fail-fast with typed
  `CategorizedError`. Read `CLAUDE.md` + `AGENTS.md`.
- Guardrails before each commit (run from the worktree root):
  - `just lint` · `just type` (whole tree) · the focused tests you added.
  - `just docs` then verify clean (`just docs-check`) **after any registrar change** — the
    tool reference is generated from registered tool params and WILL go stale when a new
    `idempotency_key` field is added; regenerate and commit it.
  - Full gate before push: `just ci` (lint, type, lint-shell, lint-workflows, check-mermaid,
    test). CI runs the sub-recipes individually, so each must pass on its own.
- TDD: write the failing test first, confirm it fails for the right reason, then minimal
  implementation, then refocus-green, then refactor green.
- Reference implementation to mirror: `services/allocation/idempotency.py`
  (`resolve_replay` / `record_key`) and its callers `allocations/request.py`,
  `services/allocation/renew.py`, plus the registrar at
  `mcp/tools/lifecycle/allocations/registrar.py:57` (the exact `Annotated` field to copy).

## Key facts established by source review (do not re-derive)

- `idempotency_keys` table (`db/schema/0002_accounting.sql:62`): PK `(principal, key)`,
  columns `kind text`, `result jsonb`, `created_at`. **No migration** — reuse as-is.
- GC already kind-agnostic: `reconciler/cleanup/gc.py:gc_idempotency_keys` deletes any row
  past retention. **No GC change.**
- `ToolResponse` (`mcp/responses.py`) is a pydantic `BaseModel`; round-trips via
  `model_dump(mode="json")` / `model_validate`.
- `queue.enqueue(conn, …)` (`jobs/queue.py:33`) opens its **own** `conn.transaction()`
  (upsert-then-fetch) and is already idempotent on `dedup_key`. So:
  - `runs.build` (`runs/server_build.py:_build_locked`) already wraps work in
    `conn.transaction()` → the record goes in that existing block (enqueue's txn nests as a
    savepoint).
  - `vmcore.fetch` (`vmcore.py:_fetch_vmcore`) and `control.power`/`control.force_crash`
    (`control.py`) call `enqueue` with **no** outer transaction → wrap `enqueue` + record in
    a new `async with conn.transaction():`.
- `ErrorCategory.CONFLICT` exists (`domain/errors.py:40`).
- Object-creating services open their own connection from `pool`:
  `services/runs/admission.py:create_run` (txn at lines 467/647),
  `services/systems/admission.py:SystemAdmission.create_for_allocation`,
  `services/catalog`/investigations `open_investigation`. These need `idempotency_key`
  threaded into the function owning the insert `conn.transaction()`.

---

## Task 1 — shared helper module + unit tests

**Files:** new `src/kdive/services/idempotency/__init__.py`,
`src/kdive/services/idempotency/envelope.py`; new
`tests/services/idempotency/test_envelope.py`.

**Do:** implement, with failing tests first:

- `validate_idempotency_key(key: str) -> None` — raise
  `CategorizedError(CONFIGURATION_ERROR, details={"reason": "idempotency_key_invalid"})`
  when `key` is empty or `len(key) > 200`.
- `async resolve_envelope_replay(conn, *, principal, key, kind) -> ToolResponse | None` —
  `SELECT result FROM idempotency_keys WHERE principal=%s AND key=%s AND kind=%s`; return
  `ToolResponse.model_validate(row[0]["envelope"])` or `None`.
- `async record_envelope(conn, *, principal, key, project, kind, envelope) -> None` —
  `INSERT … VALUES (%s,%s,%s,%s,%s)` with `Jsonb({"envelope": envelope.model_dump(mode="json")})`.
  Let `psycopg.errors.UniqueViolation` propagate (do NOT catch/map here).
- `async record_or_resolve(conn, *, principal, key, project, kind, envelope) -> ToolResponse`
  — call `record_envelope`; on `UniqueViolation`, re-run `resolve_envelope_replay` (on the
  same conn; the caller is responsible for having rolled back — see Task 2/3 note), return
  the prior envelope if found, else raise `CategorizedError(CONFLICT, details={"reason":
  "idempotency_key_in_use"})`. **Note:** because a UniqueViolation aborts the surrounding
  transaction, `record_or_resolve` is only safe to call as the final statement of a
  transaction the caller then exits; the catch/re-resolve happens after rollback. See the
  helper docstring + Task 2 for the exact call shape (the simplest correct form: the helper
  takes a freshly-resolvable conn, and topology callers structure the try/except around the
  `conn.transaction()` block — implement whichever keeps the helper a pure leaf; if the
  in-transaction-then-reresolve cannot be expressed as one leaf call, split into
  `record_envelope` + a caller-side except that calls `resolve_envelope_replay`, and drop
  `record_or_resolve`). Prefer the split if simpler — the spec's invariants, not the helper
  shape, are the contract.

**Tests (behavior, against testcontainer Postgres — mirror `tests/services/allocation`):**
- record then resolve returns an identical envelope (`model_dump` equality).
- resolve miss → `None`; resolve under a different `kind`/`principal` → `None`.
- duplicate `(principal, key)` insert raises `UniqueViolation` (record_envelope) and, via the
  caller pattern, re-resolves to the first envelope.
- `validate_idempotency_key`: empty and 201-char → `CONFIGURATION_ERROR`; 200-char ok.

**Acceptance:** `just lint`, `just type`, the new test file green. **Rollback:** delete the
new package + test; nothing else references it yet.

---

## Task 2 — job-enqueue tools (topology 1)

**Files:** `mcp/tools/lifecycle/runs/registrar.py` + `runs/server_build.py`,
`runs/server_install`/`boot` handlers (find via the registrar),
`mcp/tools/lifecycle/vmcore.py`, `mcp/tools/lifecycle/control.py`,
`mcp/tools/lifecycle/systems/registrar.py` + `systems/admin.py` (reprovision/teardown) +
`systems/provision.py` (`provision_defined`). Tests under `tests/mcp/...` mirroring each
tool's existing test module.

**Do, per tool (TDD, one tool at a time, commit per logical tool group):**
1. Registrar: add the optional `idempotency_key` `Annotated[str|None, Field(...)] = None`
   field (copy from `allocations.request`), forward to the handler.
2. Handler: after loading+authorizing the object, before the work:
   `validate_idempotency_key(key)` (if not None) then up-front `resolve_envelope_replay`;
   on hit, return it.
3. Wrap enqueue + record in one transaction:
   - `runs.build`: add `record_envelope` inside the existing `_build_locked`
     `conn.transaction()`; catch `UniqueViolation` outside `_build_locked`'s call and
     re-resolve (return winner's envelope) — or raise `CONFLICT` if a different kind.
   - `vmcore.fetch`, `control.power`, `control.force_crash`, `runs.install`, `runs.boot`,
     `systems.{provision_defined,reprovision,teardown}`: introduce
     `async with conn.transaction():` around `enqueue(...)` + `record_envelope(...)`; keep
     the catch/re-resolve outside it.
4. `control.power` only: when `idempotency_key` is supplied, build the dedup key as
   `f"{system_id}:power:{action}:{idempotency_key}"` (else keep `uuid4()`); record under
   `kind="control.power"`.
5. `kind` constant per tool = the registered tool name (one module-level constant).

**Tests (per representative tool, at minimum `vmcore.fetch` + `control.power` + one runs):**
- keyed call then keyed retry ⇒ one job (assert `COUNT(*)` of the dedup_key / one job row),
  identical envelope.
- unkeyed path unchanged (two unkeyed `control.power` ⇒ two jobs).
- key validation rejects empty/oversized before any enqueue.
- cross-tool reuse of one key ⇒ second tool returns `CONFLICT`.

**Acceptance:** focused tests green; `just docs` regenerated + committed; `just lint`/`type`.
**Rollback:** the field is additive+optional; reverting the handler edits restores prior
behavior with no data shape change.

---

## Task 3 — object-creating tools (topology 2)

**Files:** `services/runs/admission.py` (`create_run` + `_create_locked`/`_create_unbound`),
`mcp/tools/lifecycle/runs/create.py` + `runs/registrar.py`;
`services/systems/admission.py` (`create_for_allocation`) +
`mcp/tools/lifecycle/systems/provision.py` + `systems/registrar.py`;
investigations service + `mcp/tools/catalog/investigations.py`. Tests mirror each.

**Do (TDD, per tool):**
1. Registrar: add the `idempotency_key` field; forward through the MCP adapter to the
   service. The MCP adapter (`create.py`, `provision.py`) passes it down — it does NOT open
   its own connection.
2. Service: add `idempotency_key: str | None = None` parameter. At the top of the service
   (after it opens its connection, before any lock/insert): if not None,
   `validate_idempotency_key` + `resolve_envelope_replay(conn, principal=ctx.principal, …)`;
   on hit, return a result the adapter maps to the stored envelope. **Design choice:** the
   service returns its normal typed result on the work path; for replay, the cleanest is for
   the service to return the *stored `ToolResponse`* directly — but services return domain
   result types, not envelopes. Resolve this by doing the replay check + record in the **MCP
   adapter is impossible (no conn)**, so instead: have the service accept the
   `idempotency_key` and, inside its insert `conn.transaction()`, after it has the created
   object, build nothing extra — instead the service calls a small callback or returns the
   created id, and the adapter builds the envelope. To keep the envelope identical on replay,
   **record the envelope from inside the service**: pass the envelope-builder is awkward.
   **Concrete decision:** move envelope construction for these three tools to a point the
   service can reach: the service builds the success envelope itself (these envelopes are
   simple — `runs.create`'s `_created_response`, systems' `job_envelope`/
   `defined_system_envelope`, investigations' open envelope) by importing the existing
   response builders, records it inside the insert transaction, and returns the envelope (or
   a result carrying it). The MCP adapter returns the service's envelope unchanged on both
   fresh and replay paths. Keep the existing typed-error returns for the failure paths
   (failures are not recorded).
   - If relocating envelope construction into the service is too invasive for `create_run`
     (it has rich error mapping in `create.py`), an acceptable alternative: the service does
     the replay-resolve and, on a miss, performs the insert and records the envelope using a
     builder passed in by the adapter as a callable `envelope_of(result) -> ToolResponse`.
     Choose the lower-churn option; document which in the PR.
3. Record only on success, inside the insert `conn.transaction()`. Failure paths record
   nothing.
4. Concurrent duplicate: structure the try/except so a `UniqueViolation` on record rolls the
   insert back and re-resolves to the winner's envelope.

**Tests:**
- `test_runs_create_replays_on_keyed_retry` — keyed create, discard envelope, keyed retry ⇒
  exactly one `runs` row, identical envelope (the canonical acceptance test).
- mirror for `systems.provision` (one `systems` row + one PROVISION job) and
  `investigations.open` (one investigation row).
- atomicity: force the record to collide mid-flight ⇒ no second row.
- failure-not-cached: a keyed create that fails validation records no key; a corrected keyed
  call with the same key succeeds.

**Acceptance:** focused tests green; `just docs` regenerated; `just lint`/`type`.
**Rollback:** additive optional param; revert restores prior behavior, no migration.

---

## Task 4 — docs (M2 + M3) + drift guards

**Files:** `docs/guide/response-envelope.md` (M3), `docs/guide/async-jobs.md` (M2).

**Do:**
- M3: add a top-level "Idempotent retries" section (additive, NOT in the pagination region
  #620 edits — keep edits confined to a new section). State: every object-creating /
  job-enqueuing mutation accepts `idempotency_key`; a repeated key within the retention
  window replays the prior envelope; principal-scoped; recorded only on success; one key per
  logical operation (cross-operation reuse → `conflict`); ≤200 chars.
- M2: in `async-jobs.md`, document the replay/GC window (default 7 days via the reconciler
  retention config) and that a keyed enqueue replays the same job envelope within the window;
  after GC, a repeat is a fresh enqueue (still job-layer-idempotent via the object dedup key).

**Acceptance:** `just docs-links`, `just docs-paths`, `just check-mermaid` green;
`just docs` (tool reference) regenerated if any registrar changed in Tasks 2/3 and committed.
**Rollback:** prose-only; revert the section.

---

## Task 5 — full gate, branch review, security, ship

**Do:**
- `just ci` fully green from the worktree.
- `/challenge --base main` review loop; address findings.
- `security-review` on the diff; address findings.
- Push `feat/idempotency-key-619`; open PR vs `main` with `Closes #619`.
- Drive to required checks green AND `mergeStateStatus=CLEAN`/`mergeable=MERGEABLE`.
- **Do not merge** — report to the orchestrator.

**Cross-agent conflict zones to expect:** `docs/adr/README.md` (additive row already added),
the generated tool reference under `docs/guide/reference/` (regenerate on rebase), and
`docs/guide/response-envelope.md` (sibling #620 edits the pagination section — keep our
idempotency section disjoint). On `BEHIND`, rebase/merge `main`, regenerate `just docs`,
rerun `just ci`, push.
