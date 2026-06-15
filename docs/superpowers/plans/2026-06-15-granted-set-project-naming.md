# Plan — Name authorized projects in the granted-set accounting report (#426)

- **Spec:** [`../../design/granted-set-project-naming.md`](../../design/granted-set-project-naming.md)
- **ADR:** [`../../adr/0116-granted-set-project-naming.md`](../../adr/0116-granted-set-project-naming.md)
- **Branch:** `fix/granted-set-naming-426`
- **Execution mode:** direct in this session (tightly-coupled single-file change + tests;
  not worth subagent fan-out).

## Guardrails (run before every commit)

- `just lint` (ruff check + format check)
- `just type` (ty)
- `uv run pytest -q tests/mcp/accounting/test_accounting_report.py tests/services/test_accounting_report.py`
- `just docs-links` (docs touched)
- Full local gate before push: `just ci`

## Conventions

- TDD: failing test first, confirm it fails for the expected reason, minimal impl, refactor green.
- Test at the handler boundary with an injected pool + `RequestContext` (repo unit contract,
  see the existing `tests/mcp/accounting/test_accounting_report.py` header).
- 100-char lines, Google-style docstrings, absolute imports, no relative paths.
- Decimal money is `quantize_kcu`-quantized in the **domain** layer, not the tool layer.

## Task 1 — Domain helper `empty_row(project)`

**Where it fits:** the tool layer needs to synthesize a zero `RollupRow` for a granted
project with no ledger rows; quantization must stay in the domain so a zero row serializes
`"0.0000"` byte-identically to a real one.

**Files:** `src/kdive/services/accounting/ledger.py` (+ `tests/services/test_accounting_report.py`).

**Steps (TDD):**
1. Add a failing unit test: `empty_row("proj-a")` returns a `RollupRow` with
   `project="proj-a"`, `principal=None`, and `reserved == reconciled == variance ==
   quantize_kcu(Decimal(0))`, and `str(reserved) == "0.0000"`.
2. Implement `empty_row(project: str) -> RollupRow` next to `_zero_total()`, reusing
   `quantize_kcu(Decimal(0))`. Public (no leading underscore) — it is the tool layer's
   sanctioned zero-row constructor. Google-style docstring.
3. Run the focused domain test + `just type` + `just lint`.

**Acceptance:** `empty_row` returns a quantized zero row for the named project; the existing
`report()`/`_zero_total()` behaviour is unchanged.

## Task 2 — Zero-fill the granted-set response

**Where it fits:** the core fix — `accounting.report_granted_set` must name every resolved
target project, zero-filling those absent from `rollup.rows`, deterministically ordered.

**Files:** `src/kdive/mcp/tools/accounting/reports.py`
(+ `tests/mcp/accounting/test_accounting_report.py`).

**Steps (TDD):**
1. **Failing tests first** (mirror the spec's acceptance criteria):
   - `test_granted_set_zero_spend_project_is_named`: single granted project (role
     viewer) with a budget row but **no ledger rows** → `status=="ok"`,
     `project_count=="1"`, exactly one item with `project` == that name and
     `reserved/reconciled/variance == "0.0000"`.
   - Update `test_granted_set_audits_two_projects_even_when_only_one_has_spend`
     (currently asserts `{"proj-a"}`): the rows now name **both** `proj-a` (its sums)
     and `proj-b` (zero-filled). Keep the audit-count assertion (still 1) **and also
     assert the audit scope string** `rows[0][3] == "granted-set:proj-a,proj-b"` — the
     scope is derived from the *authorized set* (`sorted(targets)`, `reports.py:100`),
     independently of which projects have rows, so a future refactor that derived it
     from `rollup.rows` instead must fail here. This test pinned the *old buggy*
     behaviour; updating it is the behaviour change, not a weakening.
   - `test_granted_set_zero_fill_is_deterministically_ordered`: a granted set of three
     projects, none with spend, returns items whose `project`s are sorted ascending.
   - `test_granted_set_group_by_principal_names_zero_spend_project`: group_by=principal
     over `{proj-a (alice spend), proj-b (none)}` → `proj-b` appears once with empty
     `principal`; `proj-a` appears per-principal.
   - `test_granted_set_window_excludes_spend_names_zero`: a project with ledger rows
     only outside the window is named with `"0.0000"` zeros inside it.
2. Run the new tests; confirm they fail for the expected reason (missing names).
3. **Implement.** In the granted-set path only (`_report_granted_set`), augment the
   domain `rollup` before `_report_response`: compute `present = {row.project for row in
   rollup.rows}`, `missing = sorted(p for p in targets if p not in present)`, and build
   `rollup' = Report(rows=rollup.rows + tuple(empty_row(p) for p in missing),
   total=rollup.total)`. `_report_response` and `all-projects` are untouched. (A small
   private helper `_name_targets(rollup, targets)` in `reports.py` keeps it readable;
   it returns the rollup unchanged when nothing is missing.)
4. Confirm the new + existing report tests pass; `just lint` + `just type`.

**Acceptance:** all spec acceptance criteria for the granted-set form hold; the
all-projects tests are unchanged and the audit-count/scope assertions still pass;
`reports.py` imports and calls `accounting_domain.empty_row` (no tool-layer `Decimal`
or `RollupRow` construction), so the domain helper is the single zero-row source.

## Task 3 — Record the `allocations.list` non-bug

**Where it fits:** the issue's "record only" item — a one-line docstring note so a granted
`project` being accepted by `allocations.list` is not later "fixed" as a non-bug.

**Files:** `src/kdive/mcp/tools/lifecycle/allocations.py` (around line 295-296, the
`require_project` + `require_role(VIEWER)` site).

**Steps:**
1. Add a one-line note to the relevant docstring: accepting a granted `project` is
   working-as-designed (the caller is a viewer+ member); discovery of which projects are
   granted is `accounting.report_granted_set` (#426) / `projects.list` (#427).
2. `just lint` + `just type`.

**Acceptance:** the note exists; no behaviour change; lint/type green.

## Task 4 — Guardrails + branch review

1. Run the full `just ci` (or at minimum lint/type/test + docs-check + docs-links).
2. `just docs-check` — confirm the committed tool reference is unchanged (the advertised
   schema is the flat `{"type":"object"}` per ADR-0113, so a row-shape change must not
   alter it). If it *does* change, stop and reassess — that would mean schema leakage.
3. Adversarial-review the branch diff (`/challenge --base main`), address findings.

## Rollback / cleanup

- Pure additive logic on one code path + tests + docs; revert is a single `git revert`
  of the feature commits. No migration, no schema, no data change, nothing to undo
  operationally.

## Out of scope

- `projects.list` whoami (#427), the viewer floor, `accounting.report_all_projects` shaping.
