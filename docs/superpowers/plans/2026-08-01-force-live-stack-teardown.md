# Force live-stack teardown implementation plan

**Branch:** `feat/force-live-stack-teardown-1733`
**Base:** `main`
**Guardrail:** `just ci`

## Task 1: Pin teardown and diagnostic behavior

Add focused tests in `tests/scripts/test_live_stack_scripts.py` which prove signal failures are
reported, ordinary `stop_daemons` never escalates, `down.sh --force` performs a teardown-only
SIGKILL and refuses to stop backends after force failure, and surplus guidance names the supported
command. Run the focused pytest selection and confirm it fails for the missing behavior.

## Task 2: Implement the teardown-only force path

Update `scripts/live-stack/lib.sh` to retain the graceful stop contract while recording failed
SIGTERM delivery and to provide a separate ownership-aware forced-stop helper. Update
`scripts/live-stack/down.sh` to parse `--force`, invoke the helper only after graceful stop, and
make force failure fatal before compose teardown. Run the focused tests until green.

## Task 3: Update operator contracts

Replace the surplus-worker manual-kill guidance with `scripts/live-stack/down.sh --force`. Document
the option and its job-abandonment/reclaim consequence in `docs/operating/runbooks/live-stack.md`.
Run shell formatting/lint checks and the focused tests.

## Task 4: Verify and review

Run `just ci`. Review the branch against `main` for destructive-scope leaks, pid-race behavior,
privilege failures, and error propagation. Simplify only where behavior remains unchanged. Rollback
is a normal git revert; no persisted schema or external migration is involved.
