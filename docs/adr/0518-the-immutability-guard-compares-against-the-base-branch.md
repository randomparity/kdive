# 0518 — The schema-immutability guard compares against the base branch

## Status

Accepted (2026-07-30)

## Context

Applied migrations are byte-immutable ([ADR-0015](0015-sql-migration-runner.md)): the runner
hashes each file's whole bytes and refuses to upgrade a database whose recorded hash no longer
matches disk, so a comment reword breaks every deployment migrated by an earlier build (#1218).
ADR-0015 backed that rule with `scripts/schema_immutable_guard.py`, run as the `schema-immutable`
prek hook and from `just ci`.

The guard diffed `git diff --name-status HEAD` — the tree against itself. That is the right
question at commit time, where the edit is unstaged or staged and therefore differs from HEAD.
It is a question with only one answer anywhere else. CI's `pre-commit` job checks out a clean
tree with nothing staged, so the hook compared HEAD with HEAD, found nothing, and exited 0 on
every PR ever run, whatever the PR did to `src/kdive/db/schema/` (#1723). ADR-0015's own text
records the design as intentional: "Diffing against `HEAD` means a clean checkout passes."

So the rule was enforced by whether an author happened to run the hook on a dirty tree. Once the
edit was committed — the state every reviewer and every CI job sees — nothing looked at it again.
The `just ci` invocation had the same flaw for the same reason, and CI never runs `just ci`
anyway: it invokes justfile recipes individually, and `schema-guard` was not among them.

This is the failure mode #1720's sibling ADR ([ADR-0517](0517-migration-numbers-are-strictly-ascending-across-merges.md))
names one level up: the exit code was honest, and it was answering a question about an empty
diff. A guard that reports success over nothing is worse than no guard, because it also retires
the attention that would have caught the problem.

## Decision

The guard compares against a base ref, default `origin/main`, instead of `HEAD`. One script,
one comparison, invoked from three places that cannot disagree because none of them passes a
different base: the prek hook, `just schema-guard`, and a step of its own in
`.github/workflows/ci.yml`.

`git diff <base_ref>` compares that ref's tree against the *working tree*, so the single
comparison covers both moments the guard has to bite. On a branch, a modification already
committed still differs from `origin/main` and fails — the state CI checks out. At commit time
an unstaged or staged edit differs too, so the hook keeps the bite that caught #1218's shape.
The commit-time check is a strict subset of the CI one, not a second mechanism with its own
rules.

The prek hook keeps its place, and only that place: local commit time, where it costs
milliseconds and fails before a bad commit exists. It is added to the CI `pre-commit` job's
`SKIP` list, alongside `ty` and `lint-ansible`, for the reason those are skipped — the
lint-type-test job runs the same check properly, and that job is where the `origin/main` fetch
lives. Two copies of one gate in CI could only ever differ by one of them breaking.

Every way the comparison can come up empty is a hard failure, never a clean run: an unreadable
base ref, a base ref carrying no migrations, a cwd outside the repository. The base-carries-no-
migrations case is the one that matters most and the one the previous shape had no defence
against: measured against a tree with no `src/kdive/db/schema/`, every existing migration diffs
as an *addition*, which this guard allows by design — so an unread base passes a branch that
rewrote all of them. The reads are anchored at the repository root for the same reason, because
a `git diff` pathspec resolves against the cwd and would match nothing from a subdirectory.

The guard stays offline: it reads a local ref and never fetches (ADR-0505). Making the base ref
resolvable is the caller's job. CI fetches `refs/heads/main` at depth 1 in the step ADR-0517
already added for the ordering guard — one fetch, two guards — and locally `git fetch origin`
before `just ci` is the existing convention.

It is wired as its own step in `.github/workflows/ci.yml`, inside the required `lint · type ·
test` job. Adding it only to the `ci` recipe would gate nothing, and a new job would gate
nothing either: the ruleset requires checks by name.

## Consequences

A PR that modifies, deletes, or renames an applied migration now fails a required check. That
is the point, and it is the first time it has been true.

The comparison is against `origin/main` as it stands when the job runs, not against the PR's
merge base, matching ADR-0517. The checkout is shallow, so no merge base is computable without
deepening the whole job's history, and the ordering guard already established this base.

That choice has one false-positive shape, which the ordering guard does not share because it
only looks at added files. A migration on `origin/main` that the branch does not carry is
absent from the working tree, and the guard reports it as a deletion. Two things produce it. In
CI, `origin/main` gaining a migration between the job's checkout and its fetch step — a window
of the minute or so of dependency installation, needing a migration-adding PR to merge inside
it. Locally, an un-merged branch: a developer who fetches without merging `origin/main` sees the
phantom deletion until they do, which is the sync `just ci` before a push already assumes.
Neither is silent, both clear the same way, and the deletion message says so rather than leaving
the reader to work out that a file they never touched is being blamed on them. Requiring
branches to be up to date before merge would close the CI half along with ADR-0517's staler
variant; that is a repository-settings change tracked in #1734.

The residual is that the guard cannot distinguish the two causes, only describe both. The
distinction needs a merge base, and the merge base is exactly what a shallow checkout does not
have.

Local runs compare against whatever `origin/main` was last fetched, so a stale clone can report
a pass that CI turns into a failure. CI is the authority, as for the ordering guard.

The `find_violations` signature now takes the base ref's filenames as well as the diff, so the
empty-base hard failure is reachable from a unit test rather than only from a real repository.

## Considered & rejected

**Keep the `HEAD` diff as a second, local-only check beside a new CI guard.** Rejected. It
would be two mechanisms for one rule, differing only in a base ref, and the local one would
still score a committed violation as clean — so a developer who ran `just ci` after committing
would get a pass on the exact change CI was about to reject. Changing the base fixes the local
check as well; there was nothing worth preserving in the old comparison.

**Compare against the PR's merge base.** Exact — the merge base is precisely what the branch
forked from, which removes the false-positive window above. Rejected because the CI checkout is
shallow and no merge base exists in it; obtaining one means `fetch-depth: 0` on the whole job,
which ADR-0517 deliberately avoided in favour of fetching a single tree.

**Fetch `github.event.pull_request.base.sha` and use that as the base.** Also exact, and
shallow-fetchable. Rejected for scope: it is event-shaped, so it needs a separate fallback for
pushes to `main` and for local runs, and it would leave the two migration guards comparing
against two different bases for no stated reason.

**Add the `origin/main` fetch to the CI `pre-commit` job and let the hook enforce it there.**
Rejected. It puts the same script in two CI jobs, doubling the surface for one rule while
adding no coverage — and the `pre-commit` job is deliberately venv-free and network-light.

**Amend ADR-0015 in place.** The records gate (`.github/scripts/check-records.sh`, rule
`E-REWRITE`) holds a merged record's `## Decision` append-only, and the paragraph describing the
`HEAD` diff is exactly the prose that would have to be rewritten. Rewriting history is also the
wrong record of what happened: the old design was deliberate and its failure is the finding.
This record supersedes that paragraph, and ADR-0015 gains an appended pointer to it.
