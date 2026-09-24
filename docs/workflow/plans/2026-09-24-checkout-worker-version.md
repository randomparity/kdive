# Implementation plan: checkout worker build identity

Base: `main` at `c0ec85ac3ee3c829bdd849fdf2023e91719d17ed`.
Branch: `feat/checkout-worker-version-2737`.
Guardrails: `just lint`, `just type`, focused pytest, `just ci` before push.
Expected implementation size: 80–160 changed lines (M) — runtime resolver and skew probe with focused tests.

## Task 1: Resolve the imported checkout

Verification:

- Mode: focused-test. Contract: `version_info()` reports the imported checkout SHA independent of process cwd and checkout ownership. Test: `tests/test_version.py`; expected red: no commit from `/` or unsafe ownership. Green: `uv run python -m pytest tests/test_version.py -q` exits 0.
- Mode: focused-test. Contract: a package outside a checkout remains unknown and baked metadata wins. Test: `tests/test_version.py`; expected red: a Git parent is wrongly attributed or baked precedence is lost. Green: same command exits 0.

In `src/kdive/version.py`, derive a candidate checkout root from the module's resolved source path, require Git metadata at that root, then invoke Git against that root with a one-command `safe.directory` setting. Preserve lazy resolution, timeout, and unknown fallback. `version_info()` remains the public entry point and `_git(*args: str) -> str | None` remains the internal command seam. Add tests before changing the implementation, observe red, then green. The launcher at `scripts/live-stack/worker-from-checkout` needs no behavior change because it already sets source selection through `PYTHONPATH`. Rollback restores the resolver and tests together.

## Task 2: Explain the absent portable witness

Verification:

- Mode: focused-test. Contract: `probe_stack_skew()` describes an absent portable lifecycle witness as not deployed, excludes it from strict skew enforcement, and grades a reachable witness normally. Test: `tests/integration/live_stack/test_skew.py`; expected red: generic no-build unknown text or a strict skip. Green: `uv run python -m pytest tests/integration/live_stack/test_skew.py -q` exits 0.
- Mode: focused-test. Contract: worker version from the aux endpoint remains gradeable. Test: `tests/integration/live_stack/test_skew.py`; expected red: worker with checkout commit is unknown. Green: same command exits 0.

In `tests/integration/live_stack/skew.py`, handle the missing witness separately from a missing deployed process. Keep the other process and inventory checks unchanged. Extend the existing tests with the absent and present cases. Rollback restores the probe and its tests together.

## Integration

Run focused tests, `just lint`, and `just type` before the implementation commit. Inspect the full diff for scope and correctness. Run `just ci` on the final branch before push; the installed pre-push hook repeats the full gate. Confirm any x86 live result names the running build and commit. No ppc64le-specific execution path is changed.
