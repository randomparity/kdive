# Plan: named crash-signature presets for expected_boot_failure (#865)

- Spec: [docs/specs/2026-06-27-crash-signature-presets-865.md](../../specs/2026-06-27-crash-signature-presets-865.md)
- ADR: [ADR-0266](../../adr/0266-crash-signature-presets.md)

Tightly coupled (the matcher widening and the validator both depend on the new
`crash_signatures` module), so implement in one session via TDD. Guardrails per task:
`just lint`, `just type`, `just test` (focused first, then the relevant suites). Commit one
logical change at a time. Final: `just docs` to regenerate the tool reference, then full `just ci`
equivalents before push.

## Task 1 — crash-signatures single source of truth

**File (new):** `src/kdive/domain/lifecycle/crash_signatures.py`

Define:
- `CRASH_SIGNATURE_PRESETS: dict[str, str]` mapping each preset to its canonical `|`-pattern:
  - `"panic"` → `"Kernel panic"`
  - `"oops"` → `"Oops:|BUG: unable to handle page fault for address|BUG: kernel NULL pointer
    dereference|BUG: unable to handle kernel|kernel BUG at"`
  - `"hung_task"` → `"INFO: task |blocked for more than|blocked in I/O wait|hung_task"`
- `CONSOLE_CRASH_KIND = "console_crash"`.
- `CONSOLE_CRASH_KINDS: frozenset[str]` = `{CONSOLE_CRASH_KIND} | CRASH_SIGNATURE_PRESETS.keys()`.

**Tests (new):** `tests/domain/test_crash_signatures.py`
- Every preset pattern passes `parse_literal_terms` (≤16 terms, ≤256 chars, no empty/NUL) — the
  malformed-preset guard from the spec.
- `CONSOLE_CRASH_KINDS` == `{console_crash, oops, panic, hung_task}`.
- Spot-check representative kernel lines match each preset via `search_text` (panic line, EL8 +
  v5.0 oops lines, both hung_task variants).

**Acceptance:** module imports with no cycle; tests green.

## Task 2 — resolve presets in the domain model

**File:** `src/kdive/domain/lifecycle/records.py`

- Widen `ExpectedBootFailure.kind` to
  `Literal["console_crash", "oops", "panic", "hung_task"]`.
- Make `pattern: str | None = Field(default=None, min_length=1, max_length=256)`.
- Add `@model_validator(mode="before")` `_resolve_preset`:
  - operate only on a `dict`; return other inputs untouched.
  - if `kind` in `CRASH_SIGNATURE_PRESETS`: if `pattern` is not None → raise
    `ValueError("preset kind does not accept a custom pattern")`; else set
    `pattern = CRASH_SIGNATURE_PRESETS[kind]` (return a new dict, do not mutate the input).
  - leave `console_crash` untouched (pattern stays required → "field required" on omission).
- The existing `_literal_or_pattern` field-validator continues to run over the resolved pattern.

**Tests:** extend `tests/domain/test_models.py`
- `{kind:"panic"}` validates to `{kind:"panic", pattern:"Kernel panic"}`.
- `{kind:"oops"}` / `{kind:"hung_task"}` resolve to their canonical patterns.
- `{kind:"panic", pattern:"x"}` raises `ValidationError`.
- `{kind:"console_crash"}` (no pattern) raises `ValidationError`.
- `{kind:"console_crash", pattern:"Oops"}` round-trips unchanged.
- `{kind:"panic", description:"d"}` keeps description and resolves pattern.

**Acceptance:** model behavior matches the spec's success criteria; `ty`/`lint` clean.

## Task 3 — uniform boot matching

**File:** `src/kdive/jobs/handlers/runs/boot_evidence.py`

- Import `CONSOLE_CRASH_KINDS` from `kdive.domain.lifecycle.crash_signatures`.
- In `expected_crash_matched_line`, change the guard
  `expected.get("kind") != "console_crash"` to `expected.get("kind") not in CONSOLE_CRASH_KINDS`.
- No other change; redaction/clipping/`search_text` fail-closed behavior unchanged.

**Tests:** extend `tests/jobs/handlers/test_runs_boot.py`
- A preset-resolved doc `{kind:"panic", pattern:"Kernel panic"}` returns the matched line.
- `{kind:"oops", pattern:<canonical>}` matches an EL8 oops console line.
- The existing `{kind:"exit_code", ...}` (unknown kind) test still returns None.

**Acceptance:** matcher treats preset and custom kinds identically; existing tests stay green.

## Task 4 — tool description + admission parity

**File:** `src/kdive/mcp/tools/lifecycle/runs/registrar.py`

- Update the `expected_boot_failure` field description to document the three presets, that a
  preset takes no `pattern`, and that `console_crash` is the custom-pattern lane.

**Tests:** add an admission/create-path test (where `tests/mcp/lifecycle/test_runs_tools.py`
or `tests/services/runs/test_admission_helpers.py` already drive `_parse_expected_boot_failure`)
asserting a `{kind:"panic"}` create persists the resolved doc and a `{kind:"panic", pattern:"x"}`
create returns `configuration_error` `reason=bad_expected_boot_failure`.

**Acceptance:** create lane resolves presets and rejects preset+pattern with the existing
config-error envelope.

## Task 5 — regenerate docs + full guardrails

- Run `just docs` to regenerate `docs/guide/reference/runs.md`; review the diff.
- Run the full local gate (`just lint`, `just type`, `just docs-check`, `just test`).

**Acceptance:** `just docs-check` clean; full suite green.

## Rollback / cleanup

Pure additive feature behind validation. No migration, no persisted-state change beyond new
accepted `kind` values. Reverting the five files removes the presets; existing `console_crash`
rows are unaffected.
