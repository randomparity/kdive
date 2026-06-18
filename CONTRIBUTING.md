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
[README](README.md) for the full host-prerequisite list.

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
uv run python -m pytest tests/mcp/test_allocations_tools.py::test_name -q
```

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
  merges — flip its `Status` line and index row in that same PR. Don't change an
  accepted decision in place — write a new ADR that supersedes it. See
  [`docs/adr/README.md`](docs/adr/README.md) for the full lifecycle (including the
  partial-supersession strikethrough convention).
- Read [`docs/design/top-level-design.md`](docs/design/top-level-design.md) for
  the authoritative architecture, summarized in [ARCHITECTURE.md](ARCHITECTURE.md).
- The release process is documented in
  [`docs/development/releasing.md`](docs/development/releasing.md).
