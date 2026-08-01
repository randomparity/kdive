# Merge queue evaluation

Date: 2026-08-01
Status: implemented
ADR: [0517](../../adr/0517-migration-numbers-are-strictly-ascending-across-merges.md)

## Problem

ADR-0517 accepts a narrow race in the migration-ordering guard: a pull request can retain a
green result computed before a sibling migration merges. Its existing amendment considers
non-strict required checks but leaves GitHub's merge queue unevaluated. Issue #1753 requires
that option to be enabled or explicitly rejected.

## Decision

Do not enable a merge queue. Append the evaluation to ADR-0517 because it refines that
record's accepted residual risk; a new ADR-0532 would duplicate the decision owner.

A queue would close the race. GitHub checks out a merge-group commit containing the current
base plus the queued changes, and `GITHUB_SHA` names that commit. The existing migration
ordering guard can compare that checkout with a freshly fetched `origin/main`: a migration
behind one already merged to `main` fails, while migrations grouped in one prospective merge
are present together and apply in filename order.

The queue is rejected as disproportionate to the risk:

- every queued change would run the full required CI and records workflows a second time on
  a merge-group commit, although the stale-base hazard belongs only to concurrent migration
  additions;
- the queue preserves first-in-first-out ordering and removes or rebuilds groups after a
  failure, adding repository-wide merge latency and operational policy for a narrow race;
- build concurrency and grouping can improve throughput, but they do not remove the extra CI
  executions or make unrelated changes independent of the queue;
- current practice refreshes and rechecks remaining pull requests after serial campaign
  merges, while ADR-0517 already records the smaller residual risk outside that workflow.

## Alternatives

1. **Enable the queue now.** This gives the strongest invariant, but requires a repository
   ruleset write and workflow changes whose cost applies to every pull request.
2. **Wire `merge_group` without enabling the queue.** This carries unused workflow paths and
   cannot prove the queue behavior end to end, so it is speculative surface.
3. **Keep the queue disabled and record the evaluation.** This is the selected option. It
   satisfies #1753 without a settings write and keeps the accepted risk explicit.

## Workflow implications if revisited

Both required workflows must trigger on `merge_group`; otherwise their required contexts do
not report and the queued merge fails. `records.yml` cannot merely add the trigger because it
currently reads `github.event.pull_request.base.sha`, which is absent on a merge-group event;
it must derive a base suitable for its append-only comparison. Its concurrency key also uses
`github.event.pull_request.number`; a merge-group-safe key must use the event's ref or head SHA
so one prospective group cannot cancel another group's required check. `ci.yml`'s ordering
guard is already compatible with the merge-group checkout and fetched `origin/main`, while the
pull-request-specific immutability fetch remains conditional and its guard falls back to
`origin/main`.

## Verification

- The ADR amendment states the technical semantics, throughput tradeoff, and rejection.
- The records gate passes against `origin/main`, proving the accepted ADR remains append-only.
- `just ci` passes; no workflow or repository setting changes are made.
