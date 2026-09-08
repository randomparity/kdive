# Contributing to KDIVE

Thanks for your interest in KDIVE. This guide covers the development loop, the
branch and commit conventions, and the pull-request gate. By participating you
agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

KDIVE is Python 3.14, managed with [`uv`](https://docs.astral.sh/uv/). The
`justfile` is the single source of truth for build, lint, type, and test
commands — run the same recipes locally that CI runs.

`just setup` cannot bootstrap its own runner, so install `just` and `prek`
first:

```bash
uv tool install rust-just
uv tool install prek
just setup   # check host deps, sync the locked venv, install and run git hooks
```

`libvirt-python` has no prebuilt wheels and compiles against the system libvirt
headers, so install `libvirt-dev` (or your distro's equivalent) before `just
setup`; `just check-deps` reports any missing host packages. See the
[installation guide](docs/operating/install.md) for the full host-prerequisite list. On `ppc64le` (POWER),
`just check-deps` also requires a Rust toolchain — see the
[cross-platform development guide](docs/development/cross-platform.md) for the
per-arch prerequisites, container images, and POWER stack bring-up.

## Skipping reformat commits in `git blame`

The repo root keeps a [`.git-blame-ignore-revs`](.git-blame-ignore-revs) file
naming mechanical formatter-output commits — currently the ruff 0.16 formatter
adoption ([ADR-0569](docs/adr/0569-adopt-the-ruff-016-formatter-output-in-one-dedicated-reformat.md)).
Point `git blame` at it once per clone so those mechanical diffs don't bury the
real author of each line:

```bash
git config blame.ignoreRevsFile .git-blame-ignore-revs
```

This is a local setting and is deliberately not committed; GitHub's blame view
applies `.git-blame-ignore-revs` automatically.

## The development loop

| task        | runs                                                       |
|-------------|------------------------------------------------------------|
| `just lint` | `ruff check` + `ruff format --check`                       |
| `just format` | `ruff check --fix` + `ruff format` (mutating)            |
| `just type` | `ty check` over the whole tree (src + tests)               |
| `just test` | the suite, excluding the gated `live_vm`/`live_stack` tests |
| `just ci`   | the full PR gate                                           |

Run a single test:

```bash
uv run python -m pytest tests/mcp/lifecycle/test_allocations_tools.py::test_request_under_cap_grants -q
```

This allocation test needs Docker for disposable Postgres; it skips when Docker is unavailable.
Use `just test-changed` while iterating, `just test-lf` to rerun failures, and
`just test-verbose <test-path>` when a failure needs full context. See the
[live-testing runbook](docs/operating/runbooks/live-testing.md) for the infrastructure tiers.

Run `just ci` before you push — it runs the same recipes CI runs, so a green
local `just ci` is the baseline for a reviewable PR.

## Branch workflow

- **Never commit to `main`.** `main` is protected and changes land only through
  pull requests.
- Cut a feature branch, do the work there, and open a PR against `main`.
- Keep the branch focused on one logical change.

## Commit messages

Use [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/):

- A type prefix (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`, …).
- An imperative subject line of 72 characters or fewer (`add`, not `added`).
- One logical change per commit.

## No squash for code PRs

Code PRs are merged with rebase or merge commits — never squash. The small,
logically scoped commits exist so `git bisect` can later pin a regression to a
minimal change; squashing collapses that history into one large changeset.
Squashing is acceptable only for collapsing review iterations on non-code
artifacts (a doc or ADR that went through several review passes).

## Pull-request gate

CI runs the `just` recipes (lint, type, both doc guards, tests, and more) on the
branch head. Two conditions must both hold before a PR merges:

1. **CI is green** — every check passes.
2. **The PR is mergeable** — no conflicts with `main`. CI runs on the branch
   head, not the merge result, so a PR can show green checks while it is behind
   or conflicting against its base. If the PR is not mergeable, rebase `main`
   in, resolve conflicts, re-run `just ci`, and re-push.

Green checks alone do not mean a PR is ready to merge; it must be green **and**
mergeable.

## Architecture decisions and releases

- Architecture decisions are recorded as ADRs under `docs/adr/`. An ADR opens as
  **Proposed** and becomes **Accepted** when the PR implementing its decision
  merges — flip its `Status` in that same PR (there is no index to update: the directory
  listing is the index, ADR-0504). Don't change an
  accepted decision in place — write a new ADR that supersedes it. See
  [`docs/adr/README.md`](docs/adr/README.md) for the full lifecycle (including the
  partial-supersession amendment convention).
- Read [`docs/design/top-level-design.md`](docs/design/top-level-design.md) for
  the authoritative architecture, summarized in [ARCHITECTURE.md](ARCHITECTURE.md).
- The release process is documented in
  [`docs/development/releasing.md`](docs/development/releasing.md).

## Documentation changes

Use the [documentation index](docs/README.md) to find the guide that owns a topic. Update that
guide when behavior changes, and link to it from other pages instead of copying its procedure.
Keep exact tool parameters in the generated reference and architecture rationale in ADRs.
During a cleanup, start each document with a proposed removal. Retain it only when it has a
named audience, a distinct purpose, and claims verified against current source or recorded
historical evidence. Merge useful content into the owning guide when that removes overlap;
then remove the duplicate and repair its incoming references. A document's age or lack of
incoming links alone does not establish that its content is obsolete.

Completed task plans are working material; transfer still-useful knowledge to the owning guide
before removing them. Preserve decision records and unique verification evidence. Historical
records earn their place by explaining a decision or preserving evidence, and must be clearly
separated from instructions for the current release.

Edit generated documentation at its source: tool wrapper docstrings and parameter descriptions,
the configuration registry, or the canonical Markdown named in
[`DOC_RESOURCES`](src/kdive/mcp/resources/registrar.py). Run the corresponding `just docs`,
`just config-docs`, or `just resources-docs` recipe, then its `-check` counterpart.
For prose changes run `just docs-links`, `just docs-paths`, and `just check-mermaid`;
served documents also need `just served-doc-links` and `just resources-docs-check`.
Check Markdown formatting explicitly with `uv run ruff format --check <changed-markdown-paths>`.
Documented installation and recovery commands need verification on their supported host;
record the environment and any untested path rather than treating a link check as a live proof.
