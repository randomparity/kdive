# Implementation plan: external-build source provenance (#893)

Derived from [the spec](../../specs/external-source-provenance.md) and
[ADR-0274](../../adr/0274-external-source-provenance.md). Each task is TDD: write the failing
test first, confirm it fails for the right reason, write the minimal implementation, run the
focused test + guardrails, refactor green.

**Guardrails (run before every commit):** `just lint` (ruff check + format check), `just type`
(ty, whole tree), and the focused test module(s). Run the full `just ci` once before pushing.
CI gates `lint`/`type`/`test` individually, so each must be green on its own.

**Branch:** `feat/external-source-provenance-893` (already created). No migration, no RBAC, no
config change.

---

## Task 1 — pure validation helper `domain/external_provenance.py`

**Where it fits:** the leaf the whole feature builds on; mirrors `domain/labels.py`.

**Files:**
- new `src/kdive/domain/external_provenance.py`
- new `tests/domain/test_external_provenance.py`

**Behavior:**
- `PROVENANCE_FIELD_MAX_LEN = 256`; constants `CLIENT_ATTESTED_KEY = "client_attested"`,
  reason token `"invalid_source_provenance"`.
- `external_source_provenance(source_label: str | None, source_ref: str | None) -> dict[str, str | bool] | None`:
  - normalize each param (strip; empty → absent), matching `_normalize_cmdline`.
  - for each present field, require `1..PROVENANCE_FIELD_MAX_LEN` printable code points
    (`str.isprintable()`), else raise `CategorizedError(category=CONFIGURATION_ERROR,
    details={"reason": "invalid_source_provenance", "field": "source_label"|"source_ref"})` —
    message names the rule, never the value.
  - return `None` when neither field yields a value.
  - else return `{"client_attested": True, **{"label": ..., "source_ref": ...}}` (label key is
    `"label"`, sourced from `source_label`; `source_ref` key is `"source_ref"`).

**Tests (behavior + edges):** both None → None; whitespace-only → None; valid label only;
valid source_ref only; both → both keys + `client_attested True`; over-cap (257) → raises with
`reason`/`field`, value not in message/details; newline/control char → raises (not printable);
exactly 256 chars → accepted; surrounding whitespace trimmed in the stored value.

**Acceptance:** helper is pure, raises the uniform `CategorizedError`, names no value.

---

## Task 2 — thread provenance through the finalizer service

**Where it fits:** carries the validated dict from the handler into the recorded build step.

**Files:**
- `src/kdive/services/runs/complete_build.py`
- tests live in `tests/mcp/lifecycle/test_complete_build_tool.py` — the canonical home that
  already drives `CompleteBuildHandlers.complete_build` through the handler boundary with an
  injected `_FakeValidator` and reads the persisted build step via `_build_step_result`. There is
  no `tests/services/runs/` complete_build module; do not create one. The finalizer is exercised
  through this handler test.

**Behavior:**
- `CompleteBuildFinalizer.complete(...)` gains a keyword `source_provenance: dict[str, str | bool]
  | None = None`, passed to `_finalize_external_build`.
- `_finalize_external_build(...)` gains the same keyword and sets it on the constructed
  `BuildStepResult(build_provenance=source_provenance)`.
- No change to the already-recorded short-circuit, locks, artifact insert, or audit (`args` stays
  `{"run_id": ...}` — AC7 depends on this staying untouched).

**Tests:** finalize with a provenance dict records it on the build step result row; finalize with
`None` records no `build_provenance` (unchanged); the idempotent/already-recorded path returns the
first result and a second call's differing provenance does not overwrite it.

**Acceptance:** provenance lands in `run_steps(step='build').result.build_provenance`; default path
unchanged.

---

## Task 3 — wire the handler + MCP tool params

**Where it fits:** the agent-facing surface; validates up front and threads the dict in.

**Files:**
- `src/kdive/mcp/tools/lifecycle/runs/complete_build.py`
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` (add the two params + descriptions)
- `tests/mcp/lifecycle/test_complete_build_tool.py` (handler-level, no transport — same module as
  Task 2). Surfacing (AC1) and verbatim/audit-exclusion (AC7) are asserted by calling the existing
  `get_run` view (with `provider_resolver()` from `tests.mcp.systems_support`) and querying the
  audit table, alongside the persisted-row assertions via `_build_step_result`.

**Behavior:**
- `CompleteBuildHandlers.complete_build(...)` gains `source_label: str | None = None`,
  `source_ref: str | None = None`. After the cmdline override-token check, call
  `external_source_provenance(source_label, source_ref)`; on `CategorizedError` return
  `_config_error(run_id, data=exc.details)` (uniform envelope). Thread the result into
  `_complete_authorized_build` → `service.complete(..., source_provenance=...)`.
- Registrar `runs.complete_build` tool: add `source_label` and `source_ref` `Annotated[str | None,
  Field(description=...)]` params, descriptions making clear they are an **unverified client
  claim** recorded as provenance, opaque (not cloned/resolved), bound on first completion.

**Tests (handler boundary, injected deps):** complete with provenance → success, then `runs.get`
surfaces `data.build_provenance` with `client_attested True` + fields; complete without → no
`build_provenance`; invalid provenance → `configuration_error` with `reason=invalid_source_provenance`;
credential-like `source_ref` echoed verbatim in `runs.get` and absent from the audit row (AC7).

**Acceptance:** AC 1–7 from the spec hold at the handler boundary.

---

## Task 4 — descriptions + generated docs

**Where it fits:** keep the agent-facing prose and committed tool reference consistent.

**Files:**
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` (`runs.create` `build_profile` external
  paragraph — note optional source provenance at `runs.complete_build`)
- `src/kdive/mcp/tools/lifecycle/runs/profile_examples.py` (`_EXTERNAL_NOTE`)
- regenerate committed reference: `just docs` (and `just resources-docs` if the external-upload
  resource doc mentions completion params)

**Guard:** no `ADR-NNNN` string in any rendered description (ADR-0270 guard
`tests/mcp/core/test_no_adr_leak.py`); keep ADR citations in module docstrings/comments only.
No forbidden doc-style words.

**Acceptance:** `just docs` produces no uncommitted diff after commit; `just ci` doc gates green.

---

## Rollback / cleanup

Purely additive: new module + new optional params + reused jsonb field. Reverting the branch
removes the params and the helper with no migration or data to unwind. Existing external Runs
completed before this change simply carry no `build_provenance`, exactly as today.
