# Implementation plan: client-supplied labels on Runs and Systems (#867)

Derived from [the spec](../../specs/2026-06-27-client-labels-runs-systems-867.md) and
[ADR-0264](../../adr/0264-client-supplied-labels.md). Scope: an optional, freeform,
non-unique `label` on `runs.create`, `systems.define`, `systems.provision`, persisted
on the `runs` / `systems` rows and echoed as `data.label` in the read envelopes and the
`runs.create` success envelope.

## Conventions and guardrails (apply to every task)

- Python 3.14, `uv`. Tests mirror the package tree under `tests/`.
- TDD: write the failing test first, confirm it fails for the expected reason, then the
  minimal implementation, then re-run focused test + guardrails.
- Before each commit run the hard-gated CI recipes relevant to the change. CI invokes
  recipes **individually** (not `just ci`), so each must pass on its own:
  `just lint`, `just type` (whole tree: src + tests), `just test`, and — once a tool
  signature changes — `just docs-check` and `just adr-status-check`.
  `just check-mermaid` for docs.
- Absolute imports only; ≤100 lines/function, complexity ≤8; line length 100.
- Conventional-commit messages, imperative ≤72-char subject, ending with the
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.
- Commit one logical change at a time; keep history bisectable.

## Task 1 — Shared `validate_label` helper (pure, TDD first)

**Where it fits:** the one validation primitive both `runs.*` and `systems.*` paths
call. Service-layer validation (not a pydantic `Field` bound), because all three tools
sit behind `BindingErrorMiddleware` whose conversions match only profile errors — a
schema bound would leak a raw `ValidationError` (ADR-0247/0259).

**Files:**
- New: `src/kdive/domain/labels.py` (pure, no I/O; depends only on
  `kdive.domain.errors`).
- New test: `tests/domain/test_labels.py`.

**Contract:**
```python
LABEL_MAX_LEN = 200

def validate_label(label: str | None) -> str | None:
    """Return the cleaned label, or None. Raise CategorizedError(configuration_error,
    details={"reason": "invalid_label"}) for an invalid label (ADR-0264)."""
```
Rules (in order): `None` → return `None`; else `cleaned = label.strip()` (Unicode
whitespace); if `not (1 <= len(cleaned) <= LABEL_MAX_LEN)` → raise; if
`not cleaned.isprintable()` → raise; else return `cleaned`. The error message/`detail`
names the bound and rule only — never the rejected value (ADR-0123). Use
`ErrorCategory.CONFIGURATION_ERROR`.

**Tests (write first, must fail before impl):**
- `None` → `None`.
- `"  my run  "` → `"my run"` (interior ASCII space preserved).
- `""` and `"   "` (empty after strip) → raises, `details["reason"] == "invalid_label"`.
- `"a" * 201` → raises; `"a" * 200` → ok.
- NUL `"\x00"`, newline `"x\ny"`, tab `"x\ty"` → raise (control, Cc).
- zero-width `"a​b"` (Cf) → raises.
- bidi override `"a‮b"` (Cf) → raises.
- non-ASCII space `"a b"` (Zs) → raises.
- a non-ASCII printable label (e.g. `"café-run"`) → ok (combining/accented letters are
  printable).
- the raised error's message/detail does not contain the rejected value.

**Acceptance:** `uv run python -m pytest tests/domain/test_labels.py -q` green;
`just lint`, `just type` green.

## Task 2 — Migration 0050 + domain model fields

**Where it fits:** persistence. The repository derives insert columns from
`model_fields` (`db/repositories.py:75-86`), so adding `label` to the domain models +
the DB column makes `INSERT`/`SELECT *` carry it with no repository change.

**Files:**
- New: `src/kdive/db/schema/0050_run_system_client_label.sql`.
- Edit: `src/kdive/domain/lifecycle/records.py` — add `label: str | None = None` to
  `Run` and to `System`.
- New test: `tests/db/test_run_system_label_migration.py` (mirror
  `tests/db/test_investigation_cleanup_marker.py` / `test_build_hosts_migration.py`).

**Migration body (additive, forward-only, ADR-0015; cite ADR-0264):**
```sql
-- 0050_run_system_client_label.sql — optional client-supplied label (ADR-0264, #867).
-- Additive, forward-only (ADR-0015). Nullable; existing rows read as NULL ("no label").
ALTER TABLE runs ADD COLUMN label text;
ALTER TABLE systems ADD COLUMN label text;
```
No length CHECK in SQL — length/character validation is the service-layer
`validate_label` job (the spec's deliberate choice); the column is plain nullable
`text`.

**Tests (write first):**
- Apply the schema to a disposable Postgres (use the existing `tests/db/conftest.py`
  fixtures, which skip when Docker is absent unless `KDIVE_REQUIRE_DOCKER=1`), insert a
  `runs` and a `systems` row with and without `label`, read back: `NULL` round-trips as
  `None`, a value round-trips verbatim.
- Existing-row compatibility: a row inserted without `label` reads `None`.

**Acceptance:** migration test green; `just type` green (model change typechecks);
existing `tests/db` suite green.

## Task 3 — Thread `label` through `runs.create` (TDD)

**Where it fits:** the `runs.create` mint path and its success/echo envelope.

**Files:**
- `src/kdive/services/runs/admission.py`:
  - `RunCreateRequest` (line ~88): add `label: str | None = None`.
  - `create_run` (line ~152): as the **first step** (before `_parse_uuid`, build-profile
    parse, target resolution, locks), call `validate_label(request.label)` and on
    `CategorizedError` raise `RunCreateError(request.object_id(), ..., category=...,
    details={"reason": "invalid_label"})`. This guarantees no insert / no lock / no
    audit on an invalid label.
  - Thread the cleaned label into both Run construction sites: `_insert_run`
    (line ~540, bound path) and the unbound insert (line ~717). Add a `label` param to
    `_insert_run` and pass `label=...` to `Run(...)`.
  - `RunCreateResult` (line ~135): add `label: str | None = None`; populate it in
    `_created_result` from `run.label`.
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py`: add `label` to `_RunsCreatePayload`
  as `str | None = Field(default=None, description=...)` — **no `max_length`** (service
  validates). Pass it through `to_create_request()`.
- `src/kdive/mcp/tools/lifecycle/runs/create.py`: in `_created_response`, add
  `data["label"] = result.label`.

Note: the `RunCreateError` is converted by `ToolResponse.failure_from_error`, which
surfaces the error's safe `details` into the envelope `data`; confirm
`data["reason"] == "invalid_label"` round-trips (the Task-3 test asserts this). `_vocab_for`
only enriches `target_kind` reasons, so it leaves an `invalid_label` failure untouched.

**Tests first** (`tests/mcp/lifecycle/test_runs_tools.py`, the direct-handler boundary):
- create with a valid label → success envelope `data["label"] == "<label>"`; a
  follow-up `runs.get` shows the same (covered in Task 5 too).
- create without label → `data["label"] is None`; behavior otherwise unchanged.
- create with an invalid label (e.g. `"x\ny"`, or `"a"*201`) → `configuration_error`,
  `data["reason"] == "invalid_label"`, and **no Run row inserted** (assert the runs
  table count is unchanged / `runs.list` empty) and **no audit row** for `runs.create`.
- create with a whitespace-padded label → stored/echoed stripped.
- idempotency: same `idempotency_key`, second call with a different label → returns the
  first envelope's label (replay precedence).

**Acceptance:** focused test green; `just lint`, `just type`, `just test` green for the
touched modules.

## Task 4 — Thread `label` through `systems.define` / `systems.provision` (TDD)

**Where it fits:** both System-minting lanes. Per the spec, the System label surfaces
via `systems.get` / `systems.list` (Task 5); `systems.provision` returns a job
envelope and `systems.define` a `defined_system_envelope`, so no create-envelope echo
is required for systems.

**Validate in the handler (primary path).** Reject the invalid label in the
`provision.py` handlers, NOT in the service `AdmissionFailure` path:
`AdmissionFailureReason` (`services/systems/admission.py:116-125`) is a **closed
`StrEnum`** with no `invalid_label` member and `AdmissionFailure.reason` is typed to it,
so a service-layer rejection would force extending that enum and its
`_RECOVERY_ACTIONS` / `_admission_failure_data` maps (`provision.py:57-71`) — scope creep
the spec/ADR did not authorize. The handler path is also strictly better: it runs before
the idempotency-replay lookup and any DB connection, so "no System minted / no audit
row" holds by construction.

**Files:**
- `src/kdive/mcp/tools/lifecycle/systems/provision.py`:
  - In `provision_system` and `define_system` (lines 160-222): right after the
    `_as_uuid(allocation_id)` check and **before** `_keyed_create`, call
    `validate_label(label)`; on `CategorizedError` return
    `_config_error(allocation_id, detail="<bound/rule>", data={"reason": "invalid_label"})`
    (the `config_error(..., data=...)` shape already used in `binding_errors.py:134-138`).
    On success pass the cleaned label down through `_keyed_create` into
    `CreateSystemRequest(...)`.
  - `_keyed_create` (lines 224-247): add a `label` param threaded into
    `CreateSystemRequest(...)`.
- `src/kdive/services/systems/admission.py`:
  - `CreateSystemRequest` (line ~101): add `label: str | None = None` (carries the
    already-validated, cleaned label — persistence only, no rejection here).
  - At the `System(...)` insert site (near line ~718-720) pass `label=request.label`.
- `src/kdive/mcp/tools/lifecycle/systems/registrar.py`: add `label` Annotated param
  (`str | None = None`, description only, no `max_length`) to the `systems_define` and
  `systems_provision` tools; pass into `define_system` / `provision_system`.

**Verify the validation layering:** confirm `validate_label` runs in the handler before
`_keyed_create` (hence before the replay lookup, the allocation lock, and
`SYSTEMS.insert`), so an invalid label mints no System and writes no audit row.

**Tests first** (`tests/mcp/lifecycle/test_systems_tools.py`):
- `systems.define` and `systems.provision` with a valid label → System persisted with
  the label (assert via `systems.get` in Task 5, or read the row).
- invalid label → `configuration_error`, `reason == "invalid_label"`, no System row
  minted, no audit row.
- without label → unchanged behavior, stored `NULL`.
- idempotency replay precedence (mirror Task 3).

**Acceptance:** focused test green; `just lint`, `just type`, `just test` green.

## Task 5 — Surface `data.label` in read envelopes (TDD)

**Files:**
- `src/kdive/mcp/tools/lifecycle/runs/common.py`: in `envelope_for_run`, add
  `data["label"] = run.label` on the success path `data` dict (line ~268) AND in
  `_failed_envelope`'s `data` (line ~304) so every Run read path carries it
  (`runs.get`, `runs.list`, failed-Run). Native JSON string or `null` (ADR-0263).
- `src/kdive/mcp/tools/lifecycle/systems/view.py`: in `system_envelope`, add
  `data["label"] = system.label` in the base `data` dict (line ~67) so it appears on
  both `systems.get` and `systems.list`, success and failed paths.

**Tests first:**
- `runs.get` and `runs.list` echo `data["label"]` for a labeled Run and `null` for an
  unlabeled one; a failed Run also carries `data["label"]`.
- `systems.get` and `systems.list` echo `data["label"]` (labeled and `null`).

**Acceptance:** focused tests green.

## Task 6 — Regenerate tool reference + ADR status; full guardrails

**Where it fits:** the three tool signatures changed, so the generated agent-facing tool
reference is stale and the CI `docs-check` gate will fail otherwise.

**Steps:**
- Run `just docs` (regenerates `docs/guide/reference/runs.md` and `systems.md` from the
  live registry); review the diff is exactly the new `label` parameter; commit the
  regenerated files.
- Run `just docs-check` — must pass (no remaining drift).
- Run `just adr-status-check` — ADR-0264 must satisfy the ratification rule (it is cited
  in the migration and code docstrings).
- Run the **full** suite once: `just lint`, `just type`, `just test`, `just docs-check`,
  `just adr-status-check`, `just check-mermaid`. Fix any failure before proceeding.

**Acceptance:** every recipe above green locally.

## Rollback / cleanup

- The change is additive: a nullable column, optional params, additive `data` keys. No
  destructive migration. Rolling back the code leaves the unused nullable column
  harmless (forward-only convention, ADR-0015 — no down-migration).
- No new env var, RBAC role, or config setting to clean up.

## Sequencing

Task 1 → Task 2 (model fields depend on nothing but enable 3-5) → Tasks 3 and 4 (both
depend on 1+2; independent of each other) → Task 5 (depends on 2; verifies 3+4) →
Task 6 (depends on 3+4 having changed tool signatures). Tasks 3 and 4 may be done in
either order. Each task commits independently.
