# Remote finalize keeps a shared plan pair design

## Problem

Remote release `finalize` unlinks the plan's materialization record and plan index although
every activation of the Run shares them (#3494, ADR-0787). Another activation's prepare or
recovery then fails with a `FileNotFoundError`. Teardown collection already keeps the pair via
`keep_plan` (#3486); finalize does not.

## Scope

In: `_finalize_cleanup_quarantine`, `_activation_files`, a sibling scan in
`external_boot_authority.py`; the ADR-0787 amendment; store tests. Out: the managed re-check
(#3493, merged), the local provider (#3492), materialize/prepare/stage paths (#3131).
Current owner and intended owner are the same store; no caller migration, no obsolete path.

Finalize keeps the pair while another activation of the same Run and plan has no finalized
marker, found through `system_activations` under the store lock. The last finalize removes it.

### Failure model

- Actors and deployments: the remote authority process, one store lock per host.
- Invariants: a live activation's materialization record stays readable until it finalizes.
- Accepted: an activation with no anchor file yet is not found; an unparseable sibling anchor is not found (left in place with a warning, #3486).
- Covered elsewhere: teardown collection keeps its own `keep_plan` (#3486); the adopted-receipt
  re-check is #3493.

## Success

- Finalizing the first of two activations of one Run and plan keeps `<identity>.materialization`
  and the plan index; finalizing the second removes them.
- Finalize with no sibling, or only a sibling of another Run, another plan or already
  finalized, removes the pair.
- ADR-0787 records the rule in an appended amendment.

## Validation

- Success 1: `focused-test` `test_remote_finalize_keeps_the_plan_pair_until_the_last_activation_finalizes`,
  red when finalize passes `keep_plan=False`, green with
  `uv run python -m pytest tests/providers/remote_libvirt/test_external_boot_authority.py`.
- Success 2: `focused-test` `test_remote_finalize_removes_the_pair_when_no_sibling_needs_the_plan`
  (three cases) and the existing finalize-deletes-the-activation's-files test.
- Success 3: `task-test-not-applicable`: prose amendment, `just records` checks its form.
