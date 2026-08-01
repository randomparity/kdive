# 0517 — Migration numbers are strictly ascending across merges

## Status

Accepted (2026-07-30)

## Context

`src/kdive/db/schema/NNNN_*.sql` migrations are applied in ascending filename order and
recorded individually in `schema_migrations` (ADR-0015). `apply_migrations` skips versions it
has already recorded, so a database that has reached 0086 will happily apply an 0085 that
appears later — out of order, without complaint.

That is not hypothetical. A campaign pre-assigned 0085 to one branch and 0086 to another and
they merged in the opposite order (#1553 / #1718), so 0085 landed on `origin/main` after 0086
was already there. It was safe, but only because 0085 was a standalone `ADD COLUMN` with no
dependency on 0086 — a property nobody checked and nothing asserts.

Pre-assigning numbers to parallel branches is the right practice and prevents *filename*
collisions. It does nothing about merge order. Nothing in the tree caught the reordering:
`discover_migrations` only sorts ascending and rejects duplicate versions and malformed names,
and `scripts/schema_immutable_guard.py` only forbids modifying, deleting, or renaming an
already-applied file. Neither looks at ordering.

The failure this permits is the worst available shape. A lower-numbered migration that depends
on schema introduced by a higher-numbered one applies cleanly on a fresh database — CI creates
one per run and sees ascending order — and fails, or silently produces a different schema, on
every database that was already migrated. Green in CI, broken only in deployed environments.

The collision is introduced at authoring time, when a branch picks a number, so it should be
caught there rather than at deploy. Making `apply_migrations` refuse an out-of-order version at
runtime would turn an authoring mistake into a production startup failure, after the offending
migration is already merged and released.

## Decision

We will gate every PR on a guard, `scripts/migration_ordering_guard.py`, that fails when the
branch adds a `src/kdive/db/schema/*.sql` whose four-digit version is not **strictly greater**
than the highest version already on `origin/main`.

Strictly greater, not exactly one greater: an abandoned branch leaves its number unused, and a
gap in the sequence is harmless — every surviving migration still applies in order.

The guard compares two file *sets* — the schema directory on the base ref against the one on
disk — rather than a diff. A file already on the base ref is out of scope whatever happened to
it; that is the immutability guard's job. A newly added `*.sql` whose name does not parse as
`NNNN_*.sql` is itself a violation rather than a file the guard passes over.

Every way the comparison can come up empty is a hard failure, never a clean run: an unreadable
base ref, a base ref carrying no migrations, a missing or empty schema directory, a cwd
outside the repository. This matters more than the ordering rule itself. A guard that reports
success over nothing is worse than no guard, because it also retires the attention that would
have caught the problem (#1723), and every one of those states is a bug in how the guard was
invoked rather than evidence that the branch is clean.

The guard itself is offline: it reads a local ref and never fetches, so it reproduces exactly
from a checkout and does not put a network round-trip inside the check (ADR-0505). Making the
base ref resolvable is the caller's job — the CI job fetches `refs/heads/main` at depth 1 in
the step before, and `git fetch origin` before `just ci` is the existing local convention. The
CI gate as a whole therefore does depend on reaching github.com, but the dependency sits in a
step of its own, where a network failure is reported as a failed fetch rather than as a
migration-ordering verdict.

It is wired as its own step in `.github/workflows/ci.yml`, not only into the `ci` recipe. CI
invokes justfile recipes individually and never runs `just ci`, so a guard reachable only
through that aggregate would gate nothing.

## Consequences

Two branches that pre-assign adjacent numbers and merge out of order now fail CI on the second
one instead of merging green. The fix is mechanical — rename the file to the next free number
— and the failure message names the offending file, its version, and the current maximum.

The check is against `origin/main` as it stands when the job runs, not against the PR's merge
base, which is what lets it see a sibling that merged after the PR opened.

It does not close #1720 on its own, and the gap is worth stating plainly. A verdict is only as
fresh as the PR's last CI run: `pull_request` fires on a head change, never on a push to the
base branch, and the "protect main" ruleset does not require branches to be up to date
(`strict_required_status_checks_policy` is false). So two PRs numbered 0085 and 0086 over a
main at 0084 both go green, and if 0086 merges first, the 0085 PR keeps a green check that was
computed before the collision existed. Re-running the job, rebasing, or merging main into the
branch produces the correct red. Requiring branches to be current is what would make the guard
airtight; that is a repository-settings change, tracked separately in #1734.

Because the comparison needs `origin/main`, the guard is one of the few that is not hermetic
with respect to the checkout. Local runs use whatever `origin/main` was last fetched, so a
stale clone can report a pass that CI turns into a failure. CI is the authority.

### Amendment (2026-07-30): required checks stay non-strict, and what carries the gap (#1734)

Appended rather than substituted, because this record is merged and append-only outside
`## Status`. The paragraph above defers the up-to-date question to #1734, which is now decided —
read that sentence as pointing here. This is the decision it was waiting on.

**Required status checks on the "protect main" ruleset stay non-strict.**
`strict_required_status_checks_policy` remains `false` and no repository setting changes. Strict
mode would require every open PR to be current with `main` before it can merge, so on a repository
that merges as often as this one every open PR would need updating and re-running each time `main`
moves. That cost was judged larger than the staleness it removes.

The residual risk is the one stated above, and it is accepted rather than mitigated: a PR whose
green check was computed before a sibling migration merged can still merge past this guard. The
ordering rule is a strong gate, not an airtight one. It is also the only PR gate with that shape:
the immutability guard pins `pull_request.base.sha` (ADR-0518) and no other required check reads
`origin/main`, so #1734's wider concern — that the same reasoning applies to every check comparing
against a moving base — has no second instance here today.

A merge queue is the option this decision did not weigh. It would recompute checks against the
prospective merge result and remove the staleness without the per-PR churn strict mode imposes,
at the cost of serializing merges and of wiring `merge_group` into every required workflow.
`ci.yml` triggers on `pull_request`, on `push` to `main`, and on `workflow_dispatch`, so as it
stands the guard would not report in a queue at all — and a required context that never reports
holds the queue entry until it times out and is dropped. That blocks merges rather than letting
them through unguarded, which is the safer of the two failures but still a prerequisite to do
first. Neither evaluated nor rejected here; tracked in #1753. Non-strict stands either way.

What the gate does still hold, which is more than nothing:

- **A verdict always exists, and it is required.** `just migration-order-check` is a step of its
  own in the `lint-type-test` job (`.github/workflows/ci.yml`), and `lint · type · test` is a
  required status check on the ruleset. No PR merges without the guard having run and passed.
- **The verdict is computed against `main`'s tip, not the merge base.** The step before it
  force-fetches `+refs/heads/main:refs/remotes/origin/main` at depth 1, and
  `scripts/migration_ordering_guard.py` compares the schema directory on disk against that ref. A
  verdict computed *after* the sibling merged is correct; only one computed before it is stale.
- **Any head change recomputes it.** A push, a rebase, merging `main` into the branch (which is
  what the per-PR "Update branch" control does), or re-running the job all produce a fresh
  verdict. Non-strict makes that optional, not unavailable.
- **The exposure is narrow.** It needs two open PRs that each add a migration, merging in the
  wrong order, with no head change to the second between the first merge and its own. Narrow is
  not the same as unprecedented: `## Context` above records the first two conditions occurring in
  #1553 / #1718, before any guard existed. The third condition is what narrows it.

Two things are deliberately *not* claimed as backstops. The runtime assertion is rejected below,
so `apply_migrations` still applies an out-of-order pair without complaint on a database that has
already recorded the higher version (ADR-0015) — nothing catches this after the merge. And the
immutability guard ([ADR-0518](0518-the-immutability-guard-compares-against-the-base-branch.md))
answers a different question: it compares the branch against its own base to find an *already
applied* migration that was modified, deleted, or renamed. It does shape the remedy, though, and
the shape depends on when the mistake is caught. While the file is still an addition on a branch,
renaming it is what this guard's own message tells the author to do and the immutability guard
sees a plain `A`. Once the lower-numbered file is on `main`, the same rename is an `R` against the
base and is itself a violation — so the *post-merge* correction is a new migration above the
maximum, never a renumber.

## Considered & rejected

**Assert ascending order inside `apply_migrations`.** Fails loudly rather than proceeding, and
covers databases migrated by any build. Rejected as the primary guard: it moves the diagnosis
to deploy time, after the bad number is merged and released, and it would hard-fail startup on
databases that already applied the out-of-order pair harmlessly — including the real 0085/0086
case. Nothing stops us adding it later as defence in depth; it does not substitute for
catching the mistake at authoring time.

**Compare against the PR's merge base rather than `origin/main`.** Cheaper and reproducible
from the event payload, as `records.yml` does. Rejected because it does not catch the case that
motivated this ADR: when the sibling migration merges after the PR's base was fixed, the base
tree still shows the lower maximum and the collision passes.

**A prek hook, like `schema-immutable`.** Rejected. A commit-time hook has no fresh
`origin/main` without a network round-trip, and the CI `pre-commit` job runs on a clean
checkout with nothing staged — a hook written in that shape would be vacuous exactly where it
needs to bite (#1723). `just ci` gives the same local feedback without the false assurance.

**Document the rule in the migration-authoring guidance and stop there.** The issue proposes it
as a complement, and it is one, but the reordering here happened between two agents who were
each following the pre-assignment practice correctly. Guidance does not catch what nobody is
in a position to notice.

### Amendment (2026-08-01): a merge queue is disproportionate to the remaining risk (#1753)

The earlier amendment left GitHub's merge queue unevaluated. It is now evaluated and rejected;
the required checks remain non-strict, the repository does not require a merge queue, and the
residual race described above remains accepted.

A queue would close the race. A `merge_group` run checks out the prospective merge-group commit:
the latest target-branch state plus the queued changes ahead of and including the pull request.
The ordering guard can keep fetching `origin/main` and compare that base with the checkout. If a
higher-numbered sibling is already on `main`, the lower-numbered addition fails. If both
migrations are in one merge group, both are in the checkout and the combined merge applies them
in filename order, so their order of arrival in the queue does not create the deployed-state gap.

That stronger invariant is not free. Every queued change would run the full required CI and
records workflows again on a merge-group commit, although this stale-base hazard belongs only to
concurrent migration additions. The queue preserves first-in-first-out ordering; failed entries
are removed and later groups are rebuilt. Configurable build concurrency and grouping can run
several prospective groups at once and merge several pull requests together, but they do not
remove the additional workflow executions or the repository-wide queue policy. Current campaign
practice already merges serially and refreshes remaining pull requests after each merge. Applying
the queue's latency and operating model to every pull request is disproportionate to the narrow,
explicitly accepted risk outside that path.

Enabling it later is a coupled change, not a ruleset toggle in isolation. Both required workflows
must trigger on `merge_group`, or their required contexts never report and the queued merge fails.
The CI workflow's checkout plus its explicit `origin/main` fetch already give the ordering guard
the right comparison. The records workflow also needs event-specific base and concurrency
handling: it currently reads `github.event.pull_request.base.sha`, which a `merge_group` event
does not provide, and its gate deliberately fails in CI when `BASE_SHA` is empty. Its concurrency
key likewise uses `github.event.pull_request.number`; without an event-specific ref or head SHA,
every merge-group run would share one empty-suffixed key and newer groups would cancel older
required checks. Wiring unused triggers now is rejected as speculative surface; the workflow and
repository setting should change together if the tradeoff changes.
