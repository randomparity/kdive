# Alert on native runner lifecycle drift

## Problem

Scheduled `live-vm gates` native runs fail daily at `worker-lifecycle.sh compatibility`
because the runner's installed lifecycle protocol identity differs from the checkout's
(#3666). The only signal is a red run. Reprovisioning is an operator action (excluded).

The identity (`lifecycle_protocol_identity()`) hashes pydantic `model_json_schema()` output,
so a schema edit moves it. Bisecting main shows it first changed at 81e0690b7 (#3200,
2026-10-12) and is constant from 2026-11-05 to d0f52ad33. Why scheduled runs began failing on
2026-11-07 is not explained by repository history; it needs runner-side inspection (excluded).

## Scope

Charter: WORK:SCOPE q3666-5473ca9e on #3666. Owners stay put: `worker-lifecycle.sh` owns
the probe, `live.yml` owns CI wiring; no ownership move, no compatibility path.

1. `require_compatible_lifecycle` returns 3 for "installed protocol mismatches" and
   "installed protocol unavailable" (both mean reprovision); the checkout-probe failure stays 1.
   Only the `compatibility` subcommand surfaces the code; `request` keeps `|| return 1`.
2. The native spine runs the probe, writes `lifecycle_drift=true` to `GITHUB_OUTPUT` on 3, and
   exits with the probe's status. The probe line stays the single occurrence, before
   `cleanup_required=true` and every mutation.
3. The `native` job exposes `lifecycle_drift`. A new `lifecycle-drift` job (`needs: native`,
   `if: always() && github.event_name == 'schedule' && needs.native.outputs.lifecycle_drift == 'true'`)
   holds `issues: write` and `contents: read` only, has its own concurrency group
   (`cancel-in-progress: false`), runs `gh` with no checkout, and opens one
   issue titled `Native live_vm runner lifecycle protocol drift` unless an open issue with that
   exact title exists. Closed issues do not suppress filing: a later drift is new work.
   The body says the installed protocol does not match this checkout or could not be probed,
   links the run, and states reprovisioning is an operator action.
4. Workflow-level permissions stay `contents: read`.

### Failure model

- Actors: the scheduled workflow on the self-hosted native runner; a hosted runner for the
  report job; GitHub Issues API via `GITHUB_TOKEN`.
- Invariants: the report job gets no checkout and no project code; the probe still precedes
  every mutating step; the native job still fails on drift (the run stays red).
- Accepted: a duplicate issue from overlapping report jobs (the report job's own concurrency
  group serialises them); a `gh`/API failure fails the report job, the red run stays the signal, no retry; a drift issue not filed when the runner is offline (no job runs; the
  existing red/cancelled run is the signal); `workflow_dispatch` runs file nothing (a human is
  watching).
- Covered elsewhere: runner reprovisioning (operator); non-lifecycle scheduled failures and
  schema-PR flagging (follow-ups); the `git.kernel.org` fetch (separate issue).

## Success

1. `compatibility` exits 3 on mismatch and on an unavailable installed protocol, 1 on a failed
   checkout probe, 0 on a match.
2. On exit 3 the native step emits `lifecycle_drift=true` and the step still fails.
3. The report job runs only for scheduled runs with `lifecycle_drift == 'true'`, is the only
   job granting `issues: write`, and deduplicates on an open issue of the exact title.

## Validation

- Exit codes (1, 3): focused-test `tests/scripts/test_live_stack_scripts.py` mismatch case asserts
  returncode 3 for `compatibility`; red today (returns 1); run
  `just test-verbose tests/scripts/test_live_stack_scripts.py -k lifecycle`.
- Spine output and ordering: focused-test `tests/scripts/test_live_workflow_shape.py` asserts the
  marker, the single probe occurrence before `cleanup_required`, and exit propagation; red today.
- Report job shape and permissions: focused-test same file asserts needs/if/permissions, no
  checkout, workflow-level `contents: read`; red today (job absent).
- Dedup/issue creation shell: task-test-not-applicable for live GitHub behaviour (needs the Issues
  API); the exact-title query and `gh issue create` call are pinned structurally by the
  report-job test. `just lint-workflows` covers syntax.
