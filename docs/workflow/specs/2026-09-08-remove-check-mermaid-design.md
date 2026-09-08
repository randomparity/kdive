# Remove the check-mermaid guardrail

## Problem

The mermaid parse gate costs more than it returns (#2384). `docs-mermaid.yml` runs a Node job on
every pull request and push to main, `just setup` installs a 178 MB / 102-package `node_modules`
tree, `.github/dependabot.yml` carries an npm ecosystem for the checker alone, and
`tests/scripts/test_justfile_mermaid_recipe.py` exists only to keep the recipe's failure message
usable. It has caught one parse error (`612f8f744`) and its two upkeep issues (#2156, #1965) each
cost a fix. It guards four mermaid blocks in three files out of 1119 tracked Markdown files.

## Scope

Delete `.github/workflows/docs-mermaid.yml`, `.github/scripts/mermaid-check/`, and
`tests/scripts/test_justfile_mermaid_recipe.py`. Drop `check-mermaid` from the `justfile` `ci`
list and `install-mermaid-deps` from `setup`, with both recipes and their comments. Remove the npm
ecosystem from `.github/dependabot.yml` and the Node entry from `.gitignore`. Keep the
project-wide prose rule at `AGENTS.md:291` and drop its `check-mermaid` framing; update the gate
summary at `AGENTS.md:34`.

Two consumers the issue did not list, both compelled by the deletion:

- `CONTRIBUTING.md:133` instructs contributors to run `just check-mermaid`, which will name a
  recipe that no longer exists.
- `tests/guards/test_apt_install_is_bounded.py:255` floors the parsed job count at 17 across ten
  workflows. Removing one job makes it 16 across nine; the assertion's own message directs
  lowering the floor in the same change.

`tests/guards/test_no_workflow_pushes_to_default_branch.py:23` cites `mermaid-check.mjs` as its
example of an unread non-`.sh` script and needs a surviving one.

No replacement validation, no ADR, no ruleset edit, no diagram edits (#2384 exclusions).

## Success

1. `just ci` and `just setup` resolve with no mermaid recipe in either graph.
2. No workflow, script, dependency manifest, or Dependabot ecosystem for the checker remains.
3. `just ci` is green, including both guards the deletion touches.
4. No repository file names a mermaid recipe, script, or workflow that no longer exists.

## Validation

- Contract: the `justfile` recipe graph after removing two recipes and their two references.
  Mode: focused-test — `just --dry-run ci` and `just --dry-run setup`. Red with the recipes
  deleted and the references left (`error: justfile does not contain recipe`), green after.
- Contract: the workflow-job timeout floor in `tests/guards/test_apt_install_is_bounded.py`.
  Mode: focused-test — `uv run python -m pytest tests/guards/test_apt_install_is_bounded.py -q`.
  Red at floor 17 once `docs-mermaid.yml` is deleted, green at 16.
- Contract: pytest collection after deleting `tests/scripts/test_justfile_mermaid_recipe.py`.
  Mode: focused-test — `uv run python -m pytest tests/scripts tests/guards -q` collects and passes.
- Contract: `.gitignore` no longer ignores `node_modules/`.
  Mode: focused-test — `git check-ignore -q node_modules` exits 0 before the edit, 1 after.
- Contract: the npm ecosystem entry in `.github/dependabot.yml`.
  Mode: task-test-not-applicable — no in-repo consumer parses that file; GitHub validates it
  server-side after merge, so the only local observation is reading back bytes just written.
- Contract: prose in `AGENTS.md`, `CONTRIBUTING.md`, and the guard docstring.
  Mode: task-test-not-applicable — the change deletes clause-level prose and adds no link, path,
  or identifier an executable consumer reads. The only observation available is a repository text
  search for the removed words, which this contract forbids inventing as a test.
