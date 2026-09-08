# Implementation plan — regenerate the committed changelog at release time (#2337)

**Goal.** Stop `changelog-sync.yml` pushing a `CHANGELOG.md` commit to `main` after every merge,
move the committed changelog's regeneration into the post-release bump pull request that ADR-0041
already makes mandatory, and add a guard so no workflow silently reacquires a write path to the
default branch.

**Architecture.** `CHANGELOG.md` is rendered by `git-cliff` from conventional-commit history
(`cliff.toml`, `just changelog`). Two things consume that renderer: `release.yml`, which runs
`uvx git-cliff --latest` at tag time for the GitHub Release notes and never reads the committed
file, and — until this change — `changelog-sync.yml`. This change deletes the second consumer,
leaves the first untouched, and relocates the committed file's refresh to the
`chore(release): begin <next>-dev` pull request.

**Tech stack.** GitHub Actions workflows (YAML), `just` recipes, Python 3.14 + pytest for the
guard, Markdown for the release documentation.

Expected implementation size: 170–230 changed lines (M) — from the file map below: 91 deleted
lines in `.github/workflows/changelog-sync.yml`, ~75 new lines in the guard test, ~30 changed
lines in `docs/development/releasing.md`, and 2 in `justfile`.

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

## File map

| Path | Created / changed | Answerable for |
|---|---|---|
| `.github/workflows/changelog-sync.yml` | deleted | The per-merge regeneration and its push to `main` |
| `tests/guards/test_no_workflow_pushes_to_default_branch.py` | created | The standing guard that no workflow pushes to `main`, plus its own non-vacuity self-check |
| `docs/development/releasing.md` | changed | The post-release bump bullet, the changelog section, and the branch-protection note |
| `justfile` | changed | The `release` recipe's closing operator reminder |
| `docs/adr/0633-regenerate-the-committed-changelog-at-release-time.md` | created | The decision |
| `docs/workflow/specs/2026-09-07-changelog-release-time-regeneration-design.md` | created | The design |

## Task 1 — Guard the default branch against workflow pushes, then remove the sync

**Creates:** `tests/guards/test_no_workflow_pushes_to_default_branch.py`.
**Deletes:** `.github/workflows/changelog-sync.yml`.

**Interfaces.** Consumes nothing from another task. Uses only the standard library (`re`,
`pathlib.Path`) and pytest, matching the sibling guard
`tests/guards/test_workflow_action_pins.py`, which resolves the workflow directory as
`Path(__file__).resolve().parents[2] / ".github" / "workflows"` and enumerates it with
`sorted([*_WORKFLOWS.glob("*.yml"), *_WORKFLOWS.glob("*.yaml")])`. Both were confirmed present
with those exact forms in that file. Provides nothing to Task 2.

**Where it fits.** The whole of success criterion 1. The guard is written first so its red run
demonstrates it detects the workflow this task then deletes; without that ordering the test would
be committed already green and nothing would show it bites.

### Verification

- **Contract: no workflow pushes a commit to the default branch.**
  `Mode: focused-test` — `tests/guards/test_no_workflow_pushes_to_default_branch.py::test_no_workflow_pushes_to_the_default_branch`.
  Expected red before the deletion, naming `changelog-sync.yml:87` and `:90`; green after.
  Focused green command:
  `uv run python -m pytest tests/guards/test_no_workflow_pushes_to_default_branch.py -q`.
- **Contract: the detector is not vacuous.**
  `Mode: focused-test` — `…::test_the_detector_recognises_a_default_branch_push` asserts the
  matcher fires on the three shapes the deleted workflow used and on a bare `git push`, and does
  not fire on a tag push. It runs against literal strings, so it stays meaningful after the
  workflow is gone. Expected red if the pattern is changed to something that no longer matches.

### Steps

1. Create `tests/guards/test_no_workflow_pushes_to_default_branch.py` with exactly this content:

```python
"""Guard: no workflow pushes a commit to the default branch (ADR-0633).

`changelog-sync.yml` regenerated `CHANGELOG.md` on every push to `main` and pushed the delta
straight back over a write deploy key. That moved the default branch a second time per merge, so
every other open pull request had to refresh its base and pay a full CI cycle before it could
merge — three of the four refreshes an eight-pull-request serial batch needed (#2337). ADR-0633
removed the workflow and moved the regeneration into the post-release bump pull request. Nothing
else stops a later workflow reacquiring that write path, and the cost is invisible in the workflow
that causes it: it lands on every *other* open pull request.

This reads command text, so it is a proxy, not a proof. A push whose refspec is built from a
variable (`git push "$remote" "$ref"`) is invisible to it, and a workflow that reaches the branch
through the REST API rather than git is out of its reach entirely. What it catches is the literal
shape the removed workflow used and the shape anyone reintroducing it would write.

Stdlib + pytest only, matching `tests/guards/test_workflow_action_pins.py`: this reads the tree,
not the project.
"""

from __future__ import annotations

import re
from pathlib import Path

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

#: The repository's default branch. `docs/development/releasing.md` and the protect-main ruleset
#: both name it; it is spelled once here so the two patterns below stay in step.
_DEFAULT_BRANCH = "main"

#: Any `git push`, wherever it sits on the line — the removed workflow's was under `if !`.
_PUSH = re.compile(r"git\s+push\b(?P<args>[^\n]*)")

#: `main` as a whole ref component: `main`, `HEAD:main`, `origin/main`, `:main`, `main;`.
#: Not `domain`, `maintenance`, or `main-line`.
_DEFAULT_REF = re.compile(rf"(?<![\w-]){re.escape(_DEFAULT_BRANCH)}(?![\w-])")


def _workflow_files() -> list[Path]:
    return sorted([*_WORKFLOWS.glob("*.yml"), *_WORKFLOWS.glob("*.yaml")])


def _default_branch_pushes(text: str) -> list[str]:
    """Return each `git push` in *text* that targets the default branch, as matched."""
    offenders: list[str] = []
    for match in _PUSH.finditer(text):
        operands = [token for token in match.group("args").split() if not token.startswith("-")]
        # No operand at all means `git push` pushes the checked-out branch, which under a
        # `push: branches: [main]` trigger is the default branch.
        if not operands or any(_DEFAULT_REF.search(token) for token in operands):
            offenders.append(match.group(0).strip())
    return offenders


def test_workflow_files_are_discoverable() -> None:
    # A rename or a moved directory would make the assertion below pass over nothing.
    assert _workflow_files(), f"no workflow files found under {_WORKFLOWS}"


def test_the_detector_recognises_a_default_branch_push() -> None:
    # The three shapes `changelog-sync.yml` used before ADR-0633 removed it, plus a bare push.
    caught = _default_branch_pushes(
        'if ! git push "$remote" HEAD:main; then\n'
        '            git push "$remote" HEAD:main\n'
        "          git push origin main\n"
        "          git push\n"
    )
    assert len(caught) == 4, f"the detector stopped recognising a push to main: {caught}"
    # A tag push and a feature-branch push are not this guard's business.
    assert not _default_branch_pushes(
        'git push origin "v{{VERSION}}"\n  git push origin HEAD:refs/heads/domain-work\n'
    )


def test_no_workflow_pushes_to_the_default_branch() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _workflow_files():
        found = _default_branch_pushes(path.read_text(encoding="utf-8"))
        if found:
            offenders[path.name] = found
    assert not offenders, (
        f"a workflow pushes to {_DEFAULT_BRANCH!r}. Every such push moves the default branch "
        "outside a pull request, which forces every other open pull request through a base "
        "refresh and a full CI cycle (ADR-0633, #2337). Route the change through a reviewed "
        f"pull request instead. Offenders: {offenders}"
    )
```

2. Run the focused test and confirm it is **red**, naming `changelog-sync.yml`:

   `uv run python -m pytest tests/guards/test_no_workflow_pushes_to_default_branch.py -q`

   Expect `test_no_workflow_pushes_to_the_default_branch` to fail with
   `{'changelog-sync.yml': ['git push "$remote" HEAD:main', 'git push "$remote" HEAD:main']}`,
   and the other two tests to pass.

3. Delete the workflow: `git rm .github/workflows/changelog-sync.yml`.

4. Re-run the same command and expect `3 passed`.

5. Run `just lint` and `just type`; expect both to exit 0 with no findings.

6. Stage, record the staged set, run `prek run`, re-add exactly those paths, and commit as
   `ci(changelog): stop syncing the changelog to main on every merge`.

**Acceptance criteria.**

- `.github/workflows/changelog-sync.yml` no longer exists.
- `rg -n 'git push' .github/workflows/` returns nothing.
- The three tests in the new file pass, and the detector test fails if `_PUSH` or `_DEFAULT_REF`
  is changed to something that no longer matches `git push "$remote" HEAD:main`.
- `just lint` and `just type` are clean.

**Rollback.** `git revert` of this commit restores the workflow and reddens the guard, which is
the intended coupling: the guard cannot be satisfied while the workflow exists.

## Task 2 — Move the regeneration step into the documented release procedure

**Modifies:** `docs/development/releasing.md`, `justfile`.

**Interfaces.** Consumes nothing from Task 1 in code; it documents the mechanism Task 1 left in
place. The `justfile` symbol it edits is the `release VERSION:` recipe's final `echo`, confirmed
present as
`echo "(just set-version <next>) — CHANGELOG auto-syncs on merge; see docs/development/releasing.md."`.
The recipe's other statements (tag creation and `git push origin "v{{VERSION}}"`) are not touched.

**Where it fits.** Success criteria 2 and 3. Without it the repository ships two instructions that
fail when followed.

### Verification

- **Contract: the documented release procedure names the regeneration step.**
  `Mode: task-test-not-applicable` — the changed surface is release-procedure prose. No executable
  consumer parses it; a test asserting particular sentences would assert wording rather than
  behaviour, which this repository's plan rules forbid. The `docs-links`, `docs-paths`, and
  `served-doc-links` gates in `just ci` cover the one machine-checkable property the prose has —
  that every link it carries resolves.
- **Contract: the `just release` operator reminder.**
  `Mode: task-test-not-applicable` — one `echo` line in a `just` recipe whose surrounding
  statements push a git tag to `origin`. Observing it requires cutting a release; any structural
  observation would assert the string's wording.

### Steps

1. In `docs/development/releasing.md`, replace the "Immediately after a release" bullet so it
   runs both commands. It currently ends "You no longer run `just changelog` by hand: merging
   this PR pushes to `main`, which triggers the changelog-sync workflow (see *Changelog
   automation* below)." The replacement bullet reads:

   ```markdown
   - **Immediately after a release** — open a `chore(release): begin <next>-dev` PR that runs
     `just set-version <next-patch>` **and** `just changelog`. This is **required**: the bump is
     what keeps `X.Y.Z-dev` meaning "ahead of the last release", and the regen is the one place
     the committed changelog is refreshed
     ([ADR-0633](../adr/0633-regenerate-the-committed-changelog-at-release-time.md)). Because the
     new `vX.Y.Z` tag exists by then, git-cliff rolls the `[Unreleased]` section into the dated
     released section.
   ```

2. Before renaming or rewriting the "## Changelog automation" heading, check for inbound anchor
   links: `rg -n 'changelog-automation' -- docs README.md .github`. Keep the heading text as it
   stands if anything outside the section links to it; otherwise the heading may be retitled.
   Record which branch was taken in the commit message.

3. Replace that section's body with the four facts the spec's Scope lists: the file stays
   git-cliff-generated and never hand-edited; `just changelog` in the post-release bump PR is the
   one place it is refreshed; the committed `[Unreleased]` section is deliberately stale between
   releases and `just changelog` renders the current view locally; and nothing reads the committed
   section, because `release.yml` builds the GitHub Release notes with `git-cliff --latest` from
   git history. Add one paragraph saying a per-merge sync workflow used to do this and why it was
   removed, citing
   [ADR-0633](../adr/0633-regenerate-the-committed-changelog-at-release-time.md) and #2337.

4. Replace the section's "Branch protection" blockquote. Keep its first sentence (the ruleset:
   require-PR, required `lint · type · test`, no force-push or deletion, merge/rebase only with
   squash blocked for `git bisect`). Delete the deploy-key push description and the rotation
   instructions — that push no longer exists — and end with one sentence recording that the
   `CHANGELOG_DEPLOY_KEY` secret, the `changelog-sync (auto)` deploy key, and the ruleset's
   `DeployKey` bypass now have no consumer and are the repository owner's to remove (#2337).

5. In `justfile`, replace the `release` recipe's final `echo` with:

   ```
   echo "(just set-version <next> + just changelog) — see docs/development/releasing.md."
   ```

6. Run the documentation gates individually: `just docs-links`, `just docs-paths`,
   `just served-doc-links`. Expect each to exit 0.

7. Stage, record the staged set, run `prek run`, re-add exactly those paths, and commit as
   `docs(releasing): regenerate the changelog in the post-release bump PR`.

**Acceptance criteria.**

- `rg -n 'changelog-sync' -- docs .github justfile` returns nothing.
- `docs/development/releasing.md` names `just changelog` as a step of the post-release bump PR.
- `just release` prints a reminder naming `just changelog` and does not claim an automatic sync.
- `just docs-links`, `just docs-paths`, and `just served-doc-links` pass.

**Rollback.** `git revert` of this commit restores the previous prose; it has no runtime effect.

## Final gate

After both tasks, from the worktree root, run the full gate and read its exit status directly:

`just ci > /tmp/kdive-2337-ci.log 2>&1 < /dev/null`

Expect exit 0. The redirects give ansible-core the blocking streams `lint-ansible` requires while
leaving the recipe's own status intact.

The `records` gate runs on the pull request rather than inside `just ci`, so run it locally too —
it is what validates ADR-0633's shape, status line, and sibling links:

`RECORD_PROFILES="adr debt" BASE_SHA="$(git rev-parse origin/main)" ./.github/scripts/check-records.sh`

Expect exit 0.
