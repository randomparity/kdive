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
it; that is the immutability guard's job. A newly added file whose name does not parse as
`NNNN_*.sql` is itself a violation, so the guard has no case it silently skips.

It is offline. It reads a local ref and never fetches, so it does not make a core gate depend
on network reachability (ADR-0505). Resolving the base ref is the caller's job: the CI job
fetches `refs/heads/main` at depth 1 in the step before, and `git fetch origin` before
`just ci` is the existing local convention. An unresolvable base ref exits non-zero with the
fetch command to run — the guard never treats a missing comparison point as a pass.

It is wired as its own step in `.github/workflows/ci.yml`, not only into the `ci` recipe. CI
invokes justfile recipes individually and never runs `just ci`, so a guard reachable only
through that aggregate would gate nothing.

## Consequences

Two branches that pre-assign adjacent numbers and merge out of order now fail CI on the second
one instead of merging green. The fix is mechanical — rename the file to the next free number
— and the failure message names the offending file, its version, and the current maximum.

The check is against `origin/main` as it stands at CI time, not the PR's merge base, so a PR
can go from green to red when a sibling merges a higher-numbered migration underneath it.
That is the intended behaviour: it is exactly the #1553 situation, and the PR genuinely does
need renumbering before it lands.

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
