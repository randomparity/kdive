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

Nothing reads the committed `[Unreleased]` section. `release.yml` generates release notes with
`uvx git-cliff --latest` against git history and never opens `CHANGELOG.md`, and ADR-0041 already
records the file as cosmetic and assigns its regeneration to the post-release bump pull request.

## Scope

**In scope** — one pull request, three changes plus one consequential edit:

- **Delete `.github/workflows/changelog-sync.yml`.** The whole file. Nothing else references it:
  a repository-wide search for `changelog-sync` finds only that file and
  `docs/development/releasing.md`. No test asserts its presence;
  `tests/guards/test_workflow_action_pins.py` enumerates whatever workflows exist rather than a
  fixed list.
- **Rewrite the "Changelog automation" section of `docs/development/releasing.md`** so it states
  the mechanism that now applies: `CHANGELOG.md` stays git-cliff-generated and never hand-edited,
  `just changelog` runs in the post-release bump pull request, and `[Unreleased]` is stale between
  releases. Its branch-protection note drops the deploy-key push it described — that push no
  longer exists — and records that the `CHANGELOG_DEPLOY_KEY` secret, the `changelog-sync (auto)`
  deploy key, and the ruleset's `DeployKey` bypass are left with no consumer for the repository
  owner to remove.
- **Amend the post-release bump bullet of `docs/development/releasing.md`.** It currently reads
  "You no longer run `just changelog` by hand: merging this PR pushes to `main`, which triggers
  the changelog-sync workflow." That becomes the explicit `just changelog` step.
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
zero, and #2337 carries the follow-up.

## Success

1. `.github/workflows/changelog-sync.yml` does not exist, and no workflow in
   `.github/workflows/` pushes a commit to the default branch. Verifiable by inspecting the
   workflow set.
2. `docs/development/releasing.md` names `just changelog` as a step of the post-release
   `chore(release): begin <next>-dev` pull request, and describes no automatic per-merge sync.
3. `just release` prints a reminder consistent with (2) — it does not tell the operator the
   changelog syncs automatically on merge.
4. `CHANGELOG.md` is unmodified by this change and remains git-cliff-generated.
5. `just ci` passes, including `lint-workflows`, `docs-links`, `docs-paths`, `served-doc-links`,
   `adr-status-check`, and `just test`.

## Validation

- **Contract: the workflow set contains no push-to-default-branch job.**
  `Mode: focused-test` — `tests/guards/test_no_workflow_pushes_to_default_branch.py` parses every
  file in `.github/workflows/` and asserts none carries a `git push` to the default branch.
  Expected red before the deletion (`changelog-sync.yml` matches); green after. Focused command:
  `uv run python -m pytest tests/guards/test_no_workflow_pushes_to_default_branch.py -q`.
- **Contract: the documented release procedure names the regeneration step.**
  `Mode: task-test-not-applicable` — the changed surface is release-procedure prose in
  `docs/development/releasing.md`. Its content is read by a human, and no executable consumer
  parses it; a test asserting the presence of particular sentences would assert wording rather
  than behaviour, which this repository's plan rules forbid.
- **Contract: `just release`'s operator reminder.**
  `Mode: task-test-not-applicable` — the changed surface is one `echo` line in a `just` recipe
  whose other statements push a git tag to `origin`. Observing the line requires running a
  release; no structural observation of the string is meaningful beyond asserting its wording.
- **Contract: the ADR is a well-formed, accepted record.**
  `Mode: focused-test` — the repository's `records` gate, which runs on the pull request rather
  than inside `just ci`. Expected red for a malformed record, green for a valid one. Focused
  command:
  `RECORD_PROFILES="adr debt" BASE_SHA="$(git rev-parse origin/main)" ./.github/scripts/check-records.sh`.
