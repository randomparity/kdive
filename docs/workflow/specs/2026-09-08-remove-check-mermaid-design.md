# Remove the check-mermaid guardrail

## Problem

The mermaid parse gate costs more than it returns (#2384): a Node CI job on every pull request and
push, a `node_modules` tree in `just setup`, an npm Dependabot ecosystem, and a pytest module for
its error message. It has caught one parse error (`612f8f744`) over three mermaid blocks in two of
1119 tracked Markdown files.

## Scope

Delete `.github/workflows/docs-mermaid.yml`, `.github/scripts/mermaid-check/`, and
`tests/scripts/test_justfile_mermaid_recipe.py`. Edit `justfile` (drop `check-mermaid` from `ci`,
`install-mermaid-deps` from `setup`, and both recipes with their comments),
`.github/dependabot.yml` (npm ecosystem), `.gitignore:18-19`, `AGENTS.md:34` and `:291` (keep the
project-wide prose rule, drop its `check-mermaid` framing), and `CONTRIBUTING.md:133`.

Three consumers the issue did not list, each compelled by the deletion:

- `scripts/check-setup-deps.sh:402-404` requires npm and recommends node; `:128-130` are their
  package mappings. Left in place, `just setup` keeps demanding Node tooling nothing uses.
- `tests/guards/test_apt_install_is_bounded.py:250-255` floors parsed jobs at 17 across ten
  workflows; one job fewer makes it 16 across nine, as the assertion's own message directs.
- `tests/guards/test_no_workflow_pushes_to_default_branch.py:23` illustrates its second limb with
  `mermaid-check.mjs`. No non-`.sh` file survives under `.github/scripts/`, so the illustration
  moves to the first limb and names `./scripts/apt-install.sh` (`ci.yml:44`).

Deliberately unchanged: the generic `node_modules` filters at `.dockerignore:10`,
`test_no_workflow_pushes_to_default_branch.py:71-74`, and `test_workflow_action_pins.py:19`; and
the record locations `docs/archive/`, `docs/superpowers/`, `docs/design/`, `docs/workflow/plans/`,
and `CHANGELOG.md`. Consequence: nine merged plans keep a stale `install-mermaid-deps` line.
No replacement validation, no ADR, no ruleset edit, no diagram edits (#2384 exclusions).

## Success

1. `just ci` and `just setup` resolve with no mermaid recipe.
2. No workflow, script, manifest, Dependabot ecosystem, or host-dep check for the checker remains.
3. `just ci` is green, including both guards the deletion touches.
4. No file outside Scope's record locations names a removed recipe, script, or workflow.

## Validation

- Recipe graph — focused-test: `just --dry-run ci` and `just --dry-run setup`. Red while the
  references outlive the recipes (exit 1, ``error: recipe `ci` has unknown dependency
  `check-mermaid` ``, one global parse error shared by both); green after.
- Artifacts absent — focused-test: `just --summary | tr ' ' '\n' | rg -c
  '^(check-mermaid|install-mermaid-deps)$'` prints 2 then 0; `git ls-files
  .github/scripts/mermaid-check | wc -l` prints 4 then 0.
- Host dependencies — focused-test: `uv run python -m pytest tests/scripts/test_check_setup_deps.py
  -q` green; `./scripts/check-setup-deps.sh` names neither node nor npm.
- Workflow-job floor — focused-test: `uv run python -m pytest
  tests/guards/test_apt_install_is_bounded.py -q`. Red at 17 once the workflow is gone, green at 16.
- Collection — focused-test: `uv run python -m pytest tests/scripts tests/guards -q`.
- Ignore pattern — focused-test: `git check-ignore -q node_modules/` exits 0 then 1. The trailing
  slash is required; `.gitignore:19` is directory-only, so a bare path misses when absent.
- Dependabot structure — focused-test: `uv run python -c "import yaml;assert not [u for u in
  yaml.safe_load(open('.github/dependabot.yml'))['updates'] if u['package-ecosystem']=='npm']"`.
- Prose in `AGENTS.md`, `CONTRIBUTING.md`, and the guard docstring — task-test-not-applicable: the
  edits delete clause-level prose adding no link, path, or identifier an executable consumer reads;
  the only observation is a text search for removed words, which this contract forbids.
