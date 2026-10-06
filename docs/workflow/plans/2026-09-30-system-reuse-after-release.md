# System reuse after a completed release — plan

Goal: implement `docs/workflow/specs/2026-09-30-system-reuse-after-release-design.md` (#2968).
Architecture: a scope filter in two history searches of the authority service, tests, ADR, and
runbook text. Tech stack: Python 3, pytest, PostgreSQL testcontainers.

Expected implementation size: about 250 changed lines (M): 2 service lines, a ~200-line
connected test, ~80 lines of unit tests, the ADR, and runbook text.

## Global Constraints

- ADR number 0713 is assigned. No migration.
- Guardrails: `just lint`, `just type`, `just test-changed`, `just records`. The pre-push hook
  runs `just ci`.

## Task 1 — connected regression and service fix

Files: create `tests/integration/test_external_boot_system_reuse.py`; modify
`src/kdive/providers/external_boot_authority/service.py`.

Verification:

- Contract "a reused System passes activate and release", `Mode: focused-test`. Red before the
  fix: the second boot job stays `running` with `predecessor_operation_mismatch`. Green:
  `uv run pytest tests/integration/test_external_boot_system_reuse.py`.

Steps:

1. Write the connected test and run it. Expect red.
2. Add the `activation_id` filter to both searches. Run it. Expect green.
3. Remove each filter in turn and observe red; restore.

## Task 2 — unit guards

Files: modify `tests/providers/external_boot_authority/test_service.py`.

Verification:

- Contract "another activation's terminal record is not a predecessor", `Mode: focused-test`.
  Red without the preparation filter.
- Contract "a same-activation mismatch still refuses", `Mode: focused-test`. Green with and
  without the filter; red if the filter is inverted.

## Task 3 — decision and runbook

Files: create `docs/adr/0713-system-journal-lane-carries-successive-activations.md`; modify
`docs/operating/runbooks/live-testing.md`.

Verification: `just records` and `just lint` (markdown) green.

## Task 4 — live proof

Run the ppc64le carrier twice on one System on the validation host after the host lease grant.
Record the arms in the PR body with redacted identifiers.
