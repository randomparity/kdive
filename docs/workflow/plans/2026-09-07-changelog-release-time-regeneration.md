# Implementation plan — regenerate the committed changelog at release time (#2337)

**Goal, architecture, and rationale.** See
`docs/workflow/specs/2026-09-07-changelog-release-time-regeneration-design.md` (Problem, Scope)
and [ADR-0633](../../adr/0633-regenerate-the-committed-changelog-at-release-time.md) (Context,
Decision). In one line: delete the workflow that pushed a regenerated `CHANGELOG.md` to `main`
after every merge, move the regeneration into the post-release bump pull request, and add a
standing guard for both halves of the regression.

**Tech stack.** GitHub Actions workflows (YAML), `just` recipes, Python 3.14 + pytest for the
guard, Markdown for the release documentation.

Expected implementation size: 190–240 changed lines (M) — implementation only; the 3 design
artifacts listed in the file map (ADR, spec, this plan) are excluded from the range by design.
From the file map: 91 deleted lines in `.github/workflows/changelog-sync.yml`, ~95 new lines in
the guard test, ~35 changed lines in `docs/development/releasing.md`, and 2 in `justfile`.

## Global Constraints

- Design of record:
  [ADR-0633](../../adr/0633-regenerate-the-committed-changelog-at-release-time.md). It restores
  the release-process consequence of
  [ADR-0041](../../adr/0041-versioning-release-process.md) decision 6; ADR-0041 is not edited.
- Spec of record:
  `docs/workflow/specs/2026-09-07-changelog-release-time-regeneration-design.md`.
- Out of bounds, with owners: the protect-main ruleset's `DeployKey` bypass and the
  `CHANGELOG_DEPLOY_KEY` secret / `changelog-sync (auto)` deploy key (repository owner); required
  status checks and merge-method settings on that ruleset (repository owner); `release.yml`'s tag
  and GitHub-Release flow (ADR-0041); `cliff.toml`; `CHANGELOG.md` itself.
- Ruff line length 100, lint set `E,F,I,UP,B,SIM`. `ty` runs whole-tree with strict defaults, so
  the new test is type-checked.
- Doc-style guard (project-wide, prose included): use **Milestone**, never "Sprint"; avoid
  "critical", "robust", "comprehensive", "elegant".
- Guardrails: `just lint`, `just type`, `just test`; full gate `just ci`, run as
  `just ci > FILE 2>&1 < /dev/null` — never through a pipe, never with a trailing `; echo $?`
  (both replace the recipe's exit status).
- Before `git commit`: this change touches YAML, Markdown, shell and Python, so `just format`
  alone is not enough. Stage first, record the staged set with
  `git diff --cached --name-only`, run `prek run`, then re-add exactly those paths with
  `git add -- <paths>` — never `git add -A` or `git add -u`.
- `just adr-status-check` rejects a **Proposed** ADR cited from `src/` or `tests/`. ADR-0633 is
  written **Accepted** and this pull request fully implements it, per the ratification rule in
  `docs/adr/README.md`. There is no ADR index table to update.
- Branch `feat/drop-per-merge-changelog-sync-2337`, base `main`. Deferrals carried in: none.
- **Departure from the usual plan rule, recorded deliberately:** this plan states the guard's
  contract rather than inlining its complete source. Two independent design-review passes found
  that the inlined copy was ~28% of the plan and that the prose around it, written against a
  mental model of the code rather than the code, carried two wrong predictions. The expected
  outputs below were instead produced by executing the file. The file itself is the artifact.

## File map

| Path | Created / changed | Answerable for |
|---|---|---|
| `.github/workflows/changelog-sync.yml` | deleted | The per-merge regeneration and its push to `main` |
| `tests/guards/test_no_workflow_pushes_to_default_branch.py` | created | Two standing checks: no workflow pushes to `main`, and no operative file names a YAML file the repository lacks |
| `docs/development/releasing.md` | changed | The post-release bump bullet, the changelog section, and the branch-protection note |
| `justfile` | changed | The `release` recipe's closing operator reminder |
| `docs/adr/0633-regenerate-the-committed-changelog-at-release-time.md` | created | The decision |
| `docs/workflow/specs/2026-09-07-changelog-release-time-regeneration-design.md` | created | The design |

## Task 1 — Guard the default branch against workflow pushes, then remove the sync

**Creates:** `tests/guards/test_no_workflow_pushes_to_default_branch.py`.
**Deletes:** `.github/workflows/changelog-sync.yml`.

**Interfaces.** Consumes nothing from another task. Standard library (`re`, `subprocess`,
`pathlib.Path`) and pytest only, matching the sibling guard
`tests/guards/test_workflow_action_pins.py`, which resolves the workflow directory as
`Path(__file__).resolve().parents[2] / ".github" / "workflows"` and enumerates it with
`sorted([*_WORKFLOWS.glob("*.yml"), *_WORKFLOWS.glob("*.yaml")])`. Both were confirmed present
verbatim in that file. Provides nothing to Task 2.

**Where it fits.** The text-visible half of success criterion 1; the other half is the
pull-request requirement in the protect-main ruleset, which this change does not touch. The guard
is written first so its red run demonstrates it detects the workflow this task then deletes.

### Verification

- **Contract: no workflow pushes a commit to the default branch.**
  `Mode: focused-test` — `…::test_no_workflow_pushes_to_the_default_branch`. Expected red before
  the deletion, green after. Focused command:
  `uv run python -m pytest tests/guards/test_no_workflow_pushes_to_default_branch.py -q`.
- **Contract: neither detector is vacuous.**
  `Mode: focused-test` — `…::test_workflow_files_are_discoverable` and
  `…::test_the_detector_recognises_a_default_branch_push`. The second asserts the matcher fires on
  the three shapes the deleted workflow used and stays silent on a tag push, a lookalike branch
  name (`domain-work`), a bare `git push --tags`, and a comment. It runs against literal strings,
  so it stays meaningful after the workflow is gone.

### Steps

1. Create `tests/guards/test_no_workflow_pushes_to_default_branch.py` to this contract. It is
   stdlib + pytest, `from __future__ import annotations`, module constants then helpers then
   tests, and it reads the tree rather than importing the project.

   **Module docstring** — why the property is held directly: the per-merge push cost was invisible
   in the workflow that caused it because it landed on every *other* open pull request (#2337), so
   nothing else was going to notice it coming back. State plainly that the check is a text proxy,
   and name the case it cannot see — a marketplace commit-and-push action contains no `git push`
   text at all, and that is the shape a reacquisition would most cheaply take for as long as the
   `DeployKey` bypass survives (#2337).

   **Constants.**
   - `_ROOT = Path(__file__).resolve().parents[2]`, `_WORKFLOWS = _ROOT / ".github" / "workflows"`.
   - `_DEFAULT_BRANCH = "main"`, named once so both patterns stay in step.
   - `_PUSH = re.compile(r"git\s+push\b(?P<args>[^\n]*)")` — any `git push` wherever it sits on the
     line, because the removed workflow's sat under `if !`.
   - `_DEFAULT_REF = re.compile(rf"(?<![\w-]){re.escape(_DEFAULT_BRANCH)}(?![\w-])")` — `main` as a
     whole ref component (`HEAD:main`, `origin/main`, `:main`, `main;`), not `domain` or
     `maintenance`.

   **Helpers.**
   - `_workflow_files() -> list[Path]` — the sorted `*.yml` + `*.yaml` glob above.
   - `_strip_comments(text: str) -> str` — blanks whole-line `#` comments, so prose about a push is
     not read as one.
   - `_default_branch_pushes(text: str) -> list[str]` — over `_strip_comments(text)`, for each
     `_PUSH` match take the non-`-` operands and keep the match when any operand matches
     `_DEFAULT_REF`. Return `match.group(0).strip()`. There is deliberately **no** operand-less
     branch: flagging a bare `git push` would guess at a trigger the function does not read, and
     would redden `git push --tags` and the standard idiom for pushing to a pull-request head.

   **Tests**, exactly these three names:
   - `test_workflow_files_are_discoverable` — asserts `_workflow_files()` is non-empty.
   - `test_the_detector_recognises_a_default_branch_push` — asserts `len(caught) == 3` for the
     three removed-workflow shapes, and an empty result for the four negatives named above.
   - `test_no_workflow_pushes_to_the_default_branch` — builds `dict[str, list[str]]` keyed by
     `path.name` over `_workflow_files()` and asserts it is empty. Its message must name ADR-0633
     and #2337, say the push forces a base refresh on every other open pull request, and give the
     false-positive escape: if the push targets a pull-request head, use an explicit refspec
     (`git push origin HEAD:$BRANCH`) so the guard can tell them apart.

2. Run the focused test and confirm it is **red**. Executed against the tree at this commit, the
   exact result is `1 failed, 2 passed`, with
   `test_no_workflow_pushes_to_the_default_branch` reporting

   `Offenders: {'changelog-sync.yml': ['git push "$remote" HEAD:main; then', 'git push "$remote" HEAD:main']}`

   The first offender carries `; then` because line 87 of the workflow is
   `if ! git push "$remote" HEAD:main; then` and `_PUSH` captures to end of line. Match this
   output, not a paraphrase of it; a different failure means the file was not written to contract.

   `uv run python -m pytest tests/guards/test_no_workflow_pushes_to_default_branch.py -q`

3. Delete the workflow: `git rm .github/workflows/changelog-sync.yml`.

4. Re-run the same command and expect `3 passed`.

5. Run `just lint` and `just type`; expect both to exit 0 with no findings.

6. Stage, record the staged set, run `prek run`, re-add exactly those paths, and commit as
   `ci(changelog): stop syncing the changelog to main on every merge`.

**Acceptance criteria.**

- `.github/workflows/changelog-sync.yml` no longer exists.
- `rg -n 'git push' .github/workflows/` returns nothing.
- All three tests in `tests/guards/test_no_workflow_pushes_to_default_branch.py` pass.
- `just lint` and `just type` are clean.

**Rollback.** Task 1 is one commit, so `git revert` restores the workflow **and removes the guard
with it** — nothing goes red, and the write path returns undetected. The guard and the deletion
are not independently revertible. A deliberate revert must therefore be paired with an explicit
decision about whether to re-apply the guard.

## Task 2 — Move the regeneration step into the documented release procedure

**Modifies:** `docs/development/releasing.md`, `justfile`.

**Interfaces.** Consumes from Task 1 the red
`test_no_operative_file_names_a_missing_yaml_file`, which this task turns green by removing the
last operative reference to the deleted workflow. The `justfile` symbol it edits is the
`release VERSION:` recipe's final `echo`, confirmed present verbatim at `justfile:571` as
`echo "(just set-version <next>) — CHANGELOG auto-syncs on merge; see docs/development/releasing.md."`.
The recipe's other statements (tag creation and `git push origin "v{{VERSION}}"`) are not touched.

**Where it fits.** Success criteria 2 and 3. Without it the repository ships two instructions
that fail when followed.

### Verification

- **Contract: the release procedure and the `just release` reminder describe the mechanism that
  now applies.**
  `Mode: task-test-not-applicable` — the changed surface is a few sentences of prose and one
  `echo` string. No executable consumer parses either, and asserting their phrasing would pin
  wording rather than behaviour. This is a judgment, not a repository rule:
  `tests/guards/test_commit_hook_guidance.py` shows the repository does sometimes couple prose to
  a mechanism, and that coupling is left unenforced here. A structural check — no operative file
  may name a workflow the repository lacks — was designed and measured against the tree, then cut
  as broader than any completion criterion authorizes; it is reported as a follow-up. `just
  docs-links` and `just docs-paths` cover link resolution in the changed file. (`served-doc-links`
  does **not** — it polices only docs registered in `DOC_RESOURCES`, and `releasing.md` is not
  one.)

### Steps

1. In `docs/development/releasing.md`, replace the "Immediately after a release" bullet. It
   currently ends "You no longer run `just changelog` by hand: merging this PR pushes to `main`,
   which triggers the changelog-sync workflow (see *Changelog automation* below)." The
   replacement runs both commands and states the tag precondition:

   ```markdown
   - **Immediately after a release** — open a `chore(release): begin <next>-dev` PR that runs
     `just set-version <next-patch>`, then `git fetch --tags origin` and `just changelog`. This is
     **required**: the bump keeps `X.Y.Z-dev` meaning "ahead of the last release", and the regen is
     the one place the committed changelog is refreshed
     (ADR-0633). Fetch the
     tags first — git-cliff renders from the tags in *your* clone, and with the new `vX.Y.Z` tag
     missing it exits 0 and silently files the whole release under `[Unreleased]`. Check the diff
     shows a dated `## [X.Y.Z] - <date>` heading and an `[X.Y.Z]:` compare link in the footer; if
     `[Unreleased]` still holds the release's entries, fetch the tag and rerun.
   ```

   Render every `ADR-0633` mention in `releasing.md` as a markdown link to
   `../adr/0633-regenerate-the-committed-changelog-at-release-time.md` — that relative path is
   the correct one from `docs/development/`, and `just docs-links` resolves it.

2. Before rewriting the "## Changelog automation" heading, check for inbound anchor links:
   `rg -n 'changelog-automation' -- docs README.md .github`. Keep the heading text if anything
   outside the section links to it; otherwise it may be retitled.

3. Replace that section's body with the four facts the spec's Scope lists: the file stays
   git-cliff-generated and never hand-edited; `just changelog` in the post-release bump PR is the
   one place it is refreshed; the committed `[Unreleased]` section is deliberately stale between
   releases and `just changelog` renders the current view locally; and no automated consumer
   reads the committed section, because `release.yml` builds the GitHub Release notes with
   `git-cliff --latest` from git history. Add one sentence saying a per-merge sync workflow used to do this and why it was
   removed, citing ADR-0633 (linked as in step 1) and #2337.

4. Replace the section's "Branch protection" blockquote. Keep its first sentence (the ruleset:
   require-PR, required `lint · type · test`, no force-push or deletion, merge/rebase only with
   squash blocked for `git bisect`). Delete the deploy-key push description and the rotation
   instructions — that push no longer exists — and end with one sentence recording that the
   `CHANGELOG_DEPLOY_KEY` secret, the `changelog-sync (auto)` deploy key, and the ruleset's
   `DeployKey` bypass now have no consumer and are the repository owner's to remove (#2337).

5. In `justfile`, replace the `release` recipe's final `echo` with:

   ```
   echo "(just set-version <next>, then git fetch --tags && just changelog) — see docs/development/releasing.md."
   ```

6. Run the documentation gates: `just docs-links` and `just docs-paths` (expect exit 0 each).

7. Stage, record the staged set, run `prek run`, re-add exactly those paths, and commit as
   `docs(releasing): regenerate the changelog in the post-release bump PR`.

**Acceptance criteria.**

- `rg -n 'changelog-sync' -- .github justfile docs/development` returns exactly one hit: the
  deploy key's own name, `changelog-sync (auto)`, in the branch-protection note. That is a
  GitHub-settings identifier the repository owner needs in order to remove the key, not a
  reference to the deleted workflow file. No hit names `changelog-sync.yml`. (The ADR, spec and
  plan keep their mentions by design — they are the record of what was removed, and the `records`
  gate holds a merged ADR append-only. Scoping the grep this way is what makes the criterion
  satisfiable at all.)
- `docs/development/releasing.md` names `just changelog` as a step of the post-release bump PR and
  states the tag precondition.
- `just release` prints a reminder naming `just changelog` and does not claim an automatic sync.
- `just docs-links` and `just docs-paths` pass.

**Rollback.** `git revert` of this commit restores the previous prose. It has no runtime effect,
and no guard detects the restored staleness — that gap is the cut check, reported as a
follow-up.

## Final gate

After both tasks, from the worktree root, run the full gate and read its exit status directly:

`just ci > /tmp/kdive-2337-ci.log 2>&1 < /dev/null`

Expect exit 0. The redirects give ansible-core the blocking streams `lint-ansible` requires while
leaving the recipe's own status intact.

The `records` gate runs on the pull request rather than inside `just ci`, so run it locally too —
it is what validates ADR-0633's shape, status line, and sibling links:

`RECORD_PROFILES="adr debt" BASE_SHA="$(git rev-parse origin/main)" ./.github/scripts/check-records.sh`

Expect exit 0.
