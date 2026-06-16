# Tool-error detail surfacing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for every task — failing test first, confirm it fails for the right reason, minimal impl, refactor green.

**Goal:** Add a `detail: str | None` carrier to `ToolResponse` populated from the
`CategorizedError` message, preserve the structured `errors` list (bounded, sanitized), and
enforce a seam-level no-leak rule for `authorization_denied`/`not_found` — so every rejected tool
call is debuggable from the wire without leaking resource names. Foundation for epic #449.

**Architecture:** One reserved-key widening of `_safe_error_details`, consolidated from two copies
into **leaf modules both layers already import** (no services→mcp inversion — see the layering
note below), a `detail` field on `ToolResponse` populated under a seam suppression rule
(`suppressed_detail`), and a `detail` field on `AdmissionFailure` threaded through the
`provision.py` mapper. No DB migration — `detail` is wire-only.

**Layering (load-bearing):** no module under `kdive.services` imports `kdive.mcp` today
(verified). The consolidation must NOT introduce the first such inversion. Therefore:
- `_safe_error_details` (pure JSON sanitization, no `ErrorCategory`) moves to
  `src/kdive/serialization.py` (a zero-kdive-import leaf). `responses.py` and `admission.py`
  both import it from there.
- `suppressed_detail(category, raw)` + `_SUPPRESSED_DETAIL` (need `ErrorCategory`) live in
  `src/kdive/domain/errors.py` (a leaf both `mcp` and `services` already import). Both seams
  apply the same rule by importing it from `domain.errors`.

**Tech Stack:** Python 3.13, Pydantic v2, FastMCP. Spec:
`docs/design/tool-error-detail-surfacing.md`. ADR: `docs/adr/0123-tool-error-detail-surfacing.md`.

**Guardrails (run before every commit):** `just lint`, `just type`, and the focused tests named
per task. Run `just ci` once before pushing. Conventional-commit subjects ≤72 chars, ending with
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

- Modify: `src/kdive/serialization.py` — host the consolidated `_safe_error_details` (widened with
  the reserved `errors` key, bound, sanitization) as a public `safe_error_details`.
- Modify: `src/kdive/domain/errors.py` — `_SUPPRESSED_DETAIL` map + `suppressed_detail()` helper.
- Modify: `src/kdive/mcp/responses.py` — `detail` field, import `safe_error_details` +
  `suppressed_detail`, `detail` kwarg on `failure()`, `detail` derivation in `failure_from_error()`.
- Modify: `src/kdive/services/systems/admission.py` — delete the duplicate `_safe_error_details`
  (import `safe_error_details` from `kdive.serialization`), add `detail` to `AdmissionFailure`,
  set it via `suppressed_detail` in `_failure_from_error`.
- Modify: `src/kdive/mcp/tools/lifecycle/systems/provision.py` — `_admission_response` passes
  `result.detail` into `ToolResponse.failure`.
- Test: `tests/mcp/core/test_responses.py` (extend) — envelope-level `detail`, `errors` widening,
  bound, sanitization, seam suppression, `loc` field-path behavior.
- Test: `tests/services/systems/test_admission.py` (extend) — `AdmissionFailure.detail` populated
  and seam-suppressed; mapper threads it.
- Test: `tests/mcp/middleware` (extend, if a denial-envelope test exists) — `authorization_denied`
  detail is the constant. Else add to `tests/mcp/core/test_responses.py` via a `not_found`
  `CategorizedError` whose message embeds a name.

---

## Task 1: Envelope `detail` field + seam suppression rule

**Files:** `src/kdive/mcp/responses.py`, `tests/mcp/core/test_responses.py`

- [ ] **Step 1 (test first):** In `test_responses.py`, add:
  - `test_failure_carries_detail_kwarg` — `ToolResponse.failure(id, CONFIGURATION_ERROR,
    detail="bad thing")` → `resp.detail == "bad thing"`.
  - `test_detail_is_none_on_success` — `ToolResponse.success(...).detail is None`.
  - `test_failure_from_error_populates_detail_from_message` —
    `failure_from_error(id, CategorizedError("invalid profile", category=CONFIGURATION_ERROR))`
    → `resp.detail == "invalid profile"`.
  - `test_seam_suppresses_not_found_detail` — `CategorizedError("system <uuid> was not found",
    category=NOT_FOUND)` → `resp.detail == "not found"` AND the uuid string is absent from
    `resp.model_dump_json()`.
  - `test_seam_suppresses_authorization_denied_detail` — same shape for `AUTHORIZATION_DENIED`
    → `"access denied"`, embedded name absent.
  - `test_failure_kwarg_detail_ignored_for_suppressed_category` — `failure(id, NOT_FOUND,
    detail="leak me <name>")` → `resp.detail == "not found"`.

  Run them; confirm they fail (no `detail` field yet).

- [ ] **Step 2 (impl):** In `domain/errors.py`, add the closed map + helper:
    ```python
    _SUPPRESSED_DETAIL: dict[ErrorCategory, str] = {
        ErrorCategory.AUTHORIZATION_DENIED: "access denied",
        ErrorCategory.NOT_FOUND: "not found",
    }


    def suppressed_detail(category: ErrorCategory, raw: str | None) -> str | None:
        """Resolve the surfaced ``detail`` for ``category`` under the no-leak seam rule.

        For a suppressed category the fixed constant wins and ``raw`` is ignored, so no raise
        site can leak a resource name through ``detail``. Otherwise ``raw`` passes through.
        """
        suppressed = _SUPPRESSED_DETAIL.get(category)
        return suppressed if suppressed is not None else raw
    ```
  In `responses.py`:
  - Add `detail: str | None = None` to `ToolResponse` (after `error_category`).
  - Import `suppressed_detail` from `kdive.domain.errors`.
  - `failure()` gains `detail: str | None = None`; set
    `detail=suppressed_detail(category, detail)` on the constructed model.
  - `failure_from_error()` passes `detail=str(exc)` into `failure()` (the seam rule collapses it
    for suppressed categories).

  Run the Task-1 tests; confirm green. Run `just lint && just type`. Add an exhaustiveness test
  in `tests/domain/test_errors.py` (or `test_responses.py`) pinning `_SUPPRESSED_DETAIL` keys to
  exactly `{AUTHORIZATION_DENIED, NOT_FOUND}` so a future taxonomy edit is a deliberate diff.

## Task 2: Preserve structured `errors` (consolidate + widen filter)

**Files:** `src/kdive/mcp/responses.py`, `tests/mcp/core/test_responses.py`

- [ ] **Step 1 (test first):** Add:
  - `test_failure_from_error_preserves_structured_errors` — details
    `{"errors": [{"loc": ("provider", "kind"), "msg": "field required", "type": "missing",
    "input": "SECRET", "ctx": {...}}]}` → `resp.data["errors"] == [{"loc": ["provider",
    "kind"], "msg": "field required", "type": "missing"}]` (list `loc`, no `input`/`ctx`).
  - `test_errors_list_bounded_to_20` — feed 25 entries → exactly 20 in `resp.data["errors"]`.
  - `test_errors_entries_scalar_sanitized` — an entry with a nested dict value under an
    unexpected key is dropped to only `{loc, msg, type}`.
  - `test_loc_may_carry_caller_key_name` — an entry `loc=("MY_EXTRA_KEY",)` survives as
    `["MY_EXTRA_KEY"]` (pins the intended field-path behavior; the existing scalar tests stay
    green so non-`errors` lists are still dropped).

  Confirm they fail (`errors` currently filtered out).

- [ ] **Step 2 (impl):** Move the sanitizer to `serialization.py` as public `safe_error_details`
  and widen it:
  - The scalar loop is unchanged for every key except `errors`.
  - When `key == "errors"` and `value` is a `list`, build a sanitized list of at most 20 entries.
    For each entry that is a `Mapping`, keep only sub-keys in `{"loc", "msg", "type"}`; render
    `loc` (a tuple/list) to a `list` whose items are kept as `int` when int else `str(item)`;
    sanitize `msg`/`type` to scalars (str/bool/int/finite-float). Non-mapping entries are
    dropped. Assign the result under `safe["errors"]`.
  - Module-level constant `_MAX_ERROR_ENTRIES = 20`.
  - `responses.py` imports `safe_error_details`; `failure_from_error` calls it. The previously
    private `_safe_error_details` name in `responses.py` is removed (no shim — replace, don't
    deprecate).

  Run Task-2 tests + the existing `test_failure_from_error_carries_safe_scalar_details` /
  `test_failure_from_error_rejects_non_finite_float_detail` to confirm no regression. Lint/type.

  **Note on `bool`/`int`:** Python `isinstance(True, int)` is `True`; the existing helper already
  checks `float` first then `(str, bool, int)`, so booleans are preserved as booleans — keep that
  ordering when relocating.

## Task 3: Thread `detail` through the admission seam

**Files:** `src/kdive/services/systems/admission.py`,
`src/kdive/mcp/tools/lifecycle/systems/provision.py`,
`tests/services/systems/test_admission.py`

- [ ] **Step 1 (test first):** In `test_admission.py`, add a test that drives
  `create_for_allocation` with a malformed profile (triggers `ProvisioningProfile.parse` →
  `CategorizedError`) and asserts the returned `AdmissionFailure.detail == "invalid provisioning
  profile"` and `.data["errors"]` is a non-empty list. Add a second test that a `not_found`-shaped
  admission failure (if reachable) carries the constant — or assert at the mapper level in
  `tests/mcp/.../test provision` that a suppressed-category `AdmissionFailure` maps to the
  constant `detail`.

- [ ] **Step 2 (impl):**
  - Delete `_safe_error_details` and the `import math` it needed from `admission.py`; import
    `safe_error_details` from `kdive.serialization` (a leaf — no layering inversion).
  - Add `detail: str | None = None` to the `AdmissionFailure` dataclass.
  - `_failure_from_error` sets `detail=suppressed_detail(exc.category, str(exc))` (import from
    `kdive.domain.errors`, already imported by this module for `CategorizedError`/`ErrorCategory`).
  - In `provision.py`, `_admission_response` passes `detail=result.detail` into
    `ToolResponse.failure(...)`.

  Run Task-3 tests, lint, type.

  **Layering check:** after this task, run the layering guard
  (`tests/inventory/test_layering.py`) plus a grep that no `kdive.services` module imports
  `kdive.mcp` (`rg -ln "from kdive.mcp|import kdive.mcp" src/kdive/services/`) — it must stay
  empty. If `just type` reveals any cycle, stop and reconsider; do not paper over with a local
  import.

## Task 4: Output-schema invariant + full-suite verification

**Files:** existing `build_app` flat-output-schema test (locate via
`rg -n "ENVELOPE_OUTPUT_SCHEMA|_advertise_flat_output_schema" tests/`)

- [ ] **Step 1:** Confirm the existing flat-output-schema test stays green (the advertised schema
  is `{"type": "object"}`, unaffected by an added model field). Add one assertion that `detail`
  is not advertised as a distinct output property (i.e. the advertised schema has no
  `properties` key). If no such test exists, add a minimal one.

- [ ] **Step 2:** Update `tests/mcp/core/test_tool_docs.py` only if a tool↔test mapping requires
  it (this change adds no new tool, so likely no edit). Verify with `just test`.

- [ ] **Step 3:** Run the full `just ci` superset. Fix every warning. Regenerate any generated
  docs if `docs-check` flags drift (`just docs`), though no tool surface changed so none is
  expected.

---

## Verification checklist (before push)

- [ ] `just lint && just type && just test` green.
- [ ] `just ci` green (full hosted-CI superset).
- [ ] No DB migration added; no new tool registered.
- [ ] No-leak: a `not_found`/`authorization_denied` `CategorizedError` whose message embeds a
      name yields `detail == "not found"`/`"access denied"` and the name is absent from the
      serialized envelope.
- [ ] `errors` bounded to 20, only `{loc, msg, type}`, no `input`/`ctx`.
