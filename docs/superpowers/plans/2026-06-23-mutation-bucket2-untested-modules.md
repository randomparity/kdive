# Plan — mutation bucket 2: direct unit tests for the untested modules (#665)

Derived from [the spec](../specs/2026-06-23-mutation-bucket2-untested-modules.md) and
[ADR-0229](../../adr/0229-mutation-shim-fold-in.md). Each task is self-contained: it names the
files, the acceptance check, and the guardrail commands. Commit one logical change at a time with
the `Co-Authored-By` trailer. Guardrails for every code/test commit:

```
just lint && just type && uv run python -m pytest <touched test paths> -q
```

`just type` is whole-tree (src + tests) on purpose — never narrow it. The full `just ci` runs once
before the first push.

The mutation check for a module is:

```
just mutate src/kdive/<module>.py <new-test-path>
```

A module is done when that reports `0 surviving` (or each survivor is recorded equivalent in
`mutation-sweep-status.md`).

---

## Task 0 — Tooling fold-in (ADR-0229) — do first, it de-risks every later mutate run

**Files:** `scripts/mutate.py`, `tests/scripts/test_mutate.py`, `docs/development/mutation-testing.md`.

**Where it fits:** the mcp/middleware mutate runs (tasks 1–6) need the beartype shim; without it
their baseline aborts. Land the fold-in first so every later `just mutate` is turnkey.

**TDD steps:**
1. Add failing tests in `tests/scripts/test_mutate.py` for two new pure helpers:
   - `shim_source()` returns a `sitecustomize.py` body that imports the `multiprocessing.*`
     submodules + `beartype.claw._clawstate` + `beartype.claw._importlib._clawimpload` + `pytest`
     under a `try/except`.
   - `subprocess_env(base, shim_dir)` returns a mapping that (a) sets `UV_NO_SYNC=1`, (b) sets
     `PYTHONPATH` to `shim_dir` prepended to `base.get("PYTHONPATH")` with `os.pathsep`, preserving
     an existing value, and using just `shim_dir` when none was set.
2. Implement the helpers; thread the env through `_run_subprocess` (accept an optional `env`) and
   generate/clean the shim dir in `main()` around the existing `setup.cfg` `try/finally` (shim dir
   removed with `shutil.rmtree(..., ignore_errors=True)` even on failure).
3. Update `docs/development/mutation-testing.md`: drop the manual "export PYTHONPATH/UV_NO_SYNC"
   instructions, state the recipe now applies them.

**Acceptance:** `tests/scripts/test_mutate.py` covers env-prepend (existing + empty `PYTHONPATH`),
`UV_NO_SYNC=1`, and shim contents; `just lint && just type && uv run python -m pytest
tests/scripts/test_mutate.py -q` green. Smoke: `just mutate src/kdive/domain/errors.py
tests/domain/test_errors.py` runs to a summary with no manual env (already-clean target → 0
surviving).

**Rollback:** revert `scripts/mutate.py`; the manual workaround in the status doc still works.

---

## Tasks 1–6 — `mcp/middleware/*` (test + mutate, bucket 2a)

One task per module; each writes `tests/mcp/middleware/test_<name>.py` (create the dir + an
`__init__`-free pytest package as the tree requires), drives the module's functions directly with
fakes, and mutates to 0. Read each module first — the table is a surface hint, the code is truth.

- **Task 1 — `binding_errors.py`** → `tests/mcp/middleware/test_binding_errors.py`. Cover each
  binding-error → `CONFIGURATION_ERROR` envelope conversion + the field/loc extraction; assert the
  no-leak rule (no caller free-text in the detail).
- **Task 2 — `denial_audit.py`** → `tests/mcp/middleware/test_denial_audit.py`. Cover the denial
  audit record fields and the redaction path.
- **Task 3 — `exposure.py`** → `tests/mcp/middleware/test_exposure.py`. Cover RBAC tool-exposure
  filtering: included vs excluded tool for a role.
- **Task 4 — `shared.py`** → `tests/mcp/middleware/test_shared.py`. Cover `ToolOutcome` values,
  `result_error_category` for `ToolResponse` vs structured-dict vs neither, and `request_context`
  resolving through the package patch point.
- **Task 5 — `telemetry.py`** → `tests/mcp/middleware/test_telemetry.py`. Cover metric
  label/emission per outcome.
- **Task 6 — `usage.py`** → `tests/mcp/middleware/test_usage.py`. Cover usage-row construction per
  outcome/category.

**Acceptance (each):** new test imports the module directly; touched-test guardrails green; `just
mutate src/kdive/mcp/middleware/<name>.py tests/mcp/middleware/test_<name>.py` → 0 surviving.

---

## Tasks 7–8 — `services/runs/{admission,bind}.py` (test + mutate, bucket 2a)

- **Task 7 — `admission.py`** (640 loc, 30 defs; the largest) →
  `tests/services/runs/test_admission.py`. Drive the admission gating at the unit boundary with
  injected fakes (no Postgres). Cover admit, each reject/deny branch, and boundary inputs. This may
  need several mutate iterations; commit per coherent test cluster.
- **Task 8 — `bind.py`** → `tests/services/runs/test_bind.py`. Cover the run→system bind decision,
  including the not-bindable / wrong-state rejection.

**Acceptance (each):** direct import; guardrails green; `just mutate` → 0 surviving (record any
genuine equivalent).

---

## Task 9 — `services/runs/states.py` + `domain/lifecycle/rules.py` (data sets, bucket 2a)

→ `tests/services/runs/test_states.py`, `tests/domain/lifecycle/test_rules.py`. Assert exact set
membership for every exported `frozenset`/tuple; a mutant dropping/adding a state must fail.
Mutate each to 0.

---

## Task 10 — `inventory/_row_typing.py` (RowTyper validators, bucket 2a)

→ `tests/inventory/test_row_typing.py`. For each `RowTyper` method, cover the accept path and the
reject path (wrong type → `InventoryError` naming table/field/expected; `None` for required;
`bool`-not-`int` trap; non-`str` element in `string_list`). Mutate to 0.

---

## Task 11 — provider `settings.py` ×3 (bucket 2a)

→ `tests/providers/{fault_inject,local_libvirt,remote_libvirt}/test_settings.py`. For each
`Setting`, assert `name`, `default`, `processes`, and the `secret` flag (a flipped `secret=True`
must fail — redaction correctness). Assert each module's `SETTINGS` list membership. Mutate each
to 0.

---

## Task 12 — small data/constant modules (bucket 2a tail)

`providers/shared/build_timeouts.py` (assert `SLOW_BUILD_TOOL_TIMEOUT_S == 1800`),
`domain/catalog/image_format.py` (assert `SUPPORTED_IMAGE_FORMATS == ("qcow2",)`),
`domain/catalog/ownership.py` (assert `ManagedBy` member values),
`providers/local_libvirt/lifecycle/rootfs_catalog_fetch.py` (drive the env-fetch helper with a fake
env/fetcher), `mcp/tools/ops/_reads.py` (drive the read-projection helpers).
Co-locate tests in the mirroring `tests/` path; mutate each to 0.

---

## Task 13 — near-contract modules (bucket 2b, structural pin, expect ~0 mutants)

`db/probe_fence.py`, `providers/ports/handles.py`, `domain/_records.py`,
`diagnostics/provider_contracts.py`, `domain/profile_documents.py`, `profiles/types.py`. Write a
structural-pin test (exception identity + `__all__`; NewType/TypedDict keys; Pydantic
`extra="forbid"`/`validate_assignment` behavior; dataclass field set + frozenness; alias exports).
Run `just mutate`; if it reports `0 mutants generated`, the DoD is met by coverage; if a real
mutant appears (e.g. `extra="forbid"`), promote to 2a and kill it. Record genuine
no-mutable-surface modules in the status doc.

---

## Task 14 — status doc + close-out

Update `docs/development/mutation-sweep-status.md`: move bucket 2 from deferred to done; record the
final module count (25), any retained equivalents with reasons, and that the tooling workarounds
are now folded into the recipe (ADR-0229). Run the full `just ci` before first push.

---

## Verification gates

- Per commit: `just lint && just type && uv run python -m pytest <touched> -q` green.
- Per module: `just mutate …` → 0 surviving (or recorded equivalent).
- Before first push: full `just ci` green.
- Branch review (`/challenge --base main`) and, if required, `security-review` before ship.
