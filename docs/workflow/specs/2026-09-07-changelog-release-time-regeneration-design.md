# Regenerate the committed changelog at release time, not on every merge

- **Issue:** #2337
- **ADR:** [ADR-0633](../../adr/0633-regenerate-the-committed-changelog-at-release-time.md)
  (restores the release-process consequence of
  [ADR-0041](../../adr/0041-versioning-release-process.md) decision 6)

## Problem

`changelog-sync.yml` triggers on every push to `main` (only `CHANGELOG.md`-only pushes are
`paths-ignore`d), regenerates the changelog with `just changelog`, and pushes a
`chore(changelog): sync [Unreleased] section [skip ci]` commit back to `main` over a write
deploy key that the protect-main ruleset's `DeployKey` bypass admits.

That push moves the default branch a second time per merge. A merge process that verifies the
base is still an ancestor of the head immediately before merging — the only way to know what will
land — must then refresh every other open pull request: a merge, a full local guardrail run, a
push, and a complete CI cycle each. Campaign `aec12381` measured it across an eight-pull-request
serial batch: three of the four base refreshes the batch required were caused by this commit
alone. The tax is per open pull request and scales with merge frequency. The tip of `main` at the
time this change was branched (`1075983`) is itself one of these commits.

No automated consumer reads the committed `[Unreleased]` section. `release.yml` generates release
notes with `uvx git-cliff --latest` against git history and never opens `CHANGELOG.md`, and
ADR-0041 already
records the file as cosmetic and assigns its regeneration to the post-release bump pull request.
`pyproject.toml`'s `Changelog` project URL points a human at the file; the released sections that
reader lands on stay correct either way.

## Scope

**In scope** — one pull request, three changes plus one consequential edit:

- **Delete `.github/workflows/changelog-sync.yml`.** The whole file. Its only *operative*
  references — text that instructs or executes, as opposed to the records below that describe the
  removal — are the file itself and `docs/development/releasing.md`. No test asserts its presence;
  `tests/guards/test_workflow_action_pins.py` enumerates whatever workflows exist rather than a
  fixed list.
- **Add `tests/guards/test_no_workflow_pushes_to_default_branch.py`.** One standing structural
  check: no workflow pushes to the default branch. Nothing else holds that property, and the cost
  of losing it is invisible in the workflow that causes it — it lands on every *other* open pull
  request, which is why it went unnoticed for as long as it did.
- **Rewrite the "Changelog automation" section of `docs/development/releasing.md`** so it states
  the mechanism that now applies: `CHANGELOG.md` stays git-cliff-generated and never hand-edited,
  `just changelog` runs in the post-release bump pull request, and `[Unreleased]` is stale between
  releases. Its branch-protection note drops the deploy-key push it described — that push no
  longer exists — and records that the `CHANGELOG_DEPLOY_KEY` secret, the `changelog-sync (auto)`
  deploy key, and the ruleset's `DeployKey` bypass are left with no consumer for the repository
  owner to remove.
- **Amend the post-release bump bullet of `docs/development/releasing.md`.** It currently reads
  "You no longer run `just changelog` by hand: merging this PR pushes to `main`, which triggers
  the changelog-sync workflow." That becomes an explicit `git fetch --tags origin` then
  `just changelog`, plus the check that a dated `## [X.Y.Z]` heading appeared. The tag must be in
  the clone the regeneration runs in: the deleted workflow guaranteed that with `fetch-depth: 0`
  and `fetch-tags: true`, and with the newest tag missing git-cliff exits 0 and silently files the
  whole release under `[Unreleased]`.
- **Correct the `just release` reminder in `justfile`** (the `release` recipe's closing `echo`),
  which prints "CHANGELOG auto-syncs on merge". Left alone it is an instruction that fails when
  followed. This is a necessary consequence of the success criterion below, not new scope: no
  implementation can make the documented procedure name the regeneration step while a guardrail
  recipe tells the operator the opposite.

**Out of scope** — the ruleset's `DeployKey` bypass and the deploy key itself (repository owner);
the quest-log base-is-ancestor merge check, which is correct behaviour being taxed; required
status checks and merge-method settings on the protect-main ruleset; `release.yml`'s tag and
GitHub-Release flow, which this change does not touch; `cliff.toml`'s templates.

## Threat model

**Boundaries.** The change adds no boundary and widens none. It removes one: a GitHub Actions job
that read the `CHANGELOG_DEPLOY_KEY` secret, wrote it to `$HOME/.ssh/`, and used it to push to the
protected default branch under the ruleset's `DeployKey` bypass — the repository's only automated
write path to `main`.

**Actors.** Anyone who can cause a push to `main` (a maintainer merging a pull request) could
cause that job to run; anyone able to alter a workflow file the job invoked (`just changelog`,
`cliff.toml`) could influence what it committed. After the change, reaching `main` requires a
reviewed pull request in every case.

**Controls.** The removed job's controls — `permissions: contents: read`, `persist-credentials:
false`, `enable-cache: false`, the serialized `concurrency` group — go away with the job that
needed them. The control that remains is the protect-main ruleset itself, unchanged.

**Out of scope, stated.** The secret and deploy key are not revoked here (repository owner), so
between this merge and that removal a write bypass exists with no consumer. That is strictly
narrower than today, where the same bypass exists *and* is exercised on every merge, but it is not
zero. It needs an owner that outlives #2337, which this change closes, so it is tracked by
[deferral record 0012](../../debt/0012-changelog-write-deploy-key-and-bypass-have-no-consumer.md)
rather than deferred to the issue being resolved here.

## Success

1. `.github/workflows/changelog-sync.yml` does not exist, and no workflow in
   `.github/workflows/` pushes a commit to the default branch. Verifiable by inspecting the
   workflow set.
2. `docs/development/releasing.md` names `just changelog` as a step of the post-release
   `chore(release): begin <next>-dev` pull request, states the tag precondition, and describes no
   automatic per-merge sync.
3. `just release` prints a reminder consistent with (2) — it does not tell the operator the
   changelog syncs automatically on merge.
4. `CHANGELOG.md` is unmodified by this change and remains git-cliff-generated.
5. `just ci` passes, including `lint-workflows`, `docs-links`, `docs-paths`, `adr-status-check`,
   and `just test`.

## Validation

- **Contract: the workflow set contains no push-to-default-branch job.**
  `Mode: focused-test` — `tests/guards/test_no_workflow_pushes_to_default_branch.py` parses every
  file in `.github/workflows/`, and every shell script under `.github/scripts/` that a workflow
  can invoke, and asserts none carries a `git push` to the default branch.
  Expected red before the deletion (`changelog-sync.yml` matches); green after. Focused command:
  `uv run python -m pytest tests/guards/test_no_workflow_pushes_to_default_branch.py -q`.
- **Contract: the release procedure and the `just release` reminder describe the mechanism that
  now applies.**
  `Mode: task-test-not-applicable` — the changed surface is a few sentences of prose in
  `docs/development/releasing.md` and one `echo` string in a `just` recipe. No executable consumer
  parses either, and asserting their phrasing would pin wording rather than behaviour. This is a
  judgment, not a repository rule: `tests/guards/test_commit_hook_guidance.py` shows the
  repository does sometimes couple prose to a mechanism, and that coupling is left unenforced
  here. A structural check — no operative file may name a workflow the repository lacks — was
  designed, measured against the tree, and cut: it is broader than any completion criterion
  authorizes, so it is reported as a follow-up rather than smuggled in. `just docs-links` and
  `just docs-paths` still cover link resolution in the changed file.
- **Contract: the ADR is a well-formed, accepted record.**
  `Mode: focused-test` — the repository's `records` gate, which runs on the pull request rather
  than inside `just ci`. Expected red for a malformed record, green for a valid one. Focused
  command:
  `RECORD_PROFILES="adr debt" BASE_SHA="$(git rev-parse origin/main)" ./.github/scripts/check-records.sh`.
