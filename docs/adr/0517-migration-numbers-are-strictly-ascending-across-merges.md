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
