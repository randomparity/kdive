# 0672 — Developer hosts supply Bash 4.4 and GNU tools on PATH

## Status

Accepted (2026-09-22)

## Context

Issue #2647 found four developer-loop paths that fail on stock macOS, where
`#!/usr/bin/env bash` resolves `/bin/bash` 3.2.57: `just setup`, `just served-doc-links`
(and so the ADR-0670 pre-push `just ci`), `just test-ansible`, and `just test-changed`.
The scripts use Bash 4 features (`local -n`, `mapfile`, associative arrays) and GNU tool
flags (`realpath -m`, `stat -c`). The failures appear deep in a run, not at setup.

## Decision

A developer host supplies Bash >= 4.4 and GNU coreutils, findutils, and grep on `PATH`.
On macOS these come from Homebrew, with their `bin` and `gnubin` directories ahead of the
system directories. `just check-deps` enforces the floor: it stops before any other probe
when the running interpreter or the `bash` on `PATH` is older than 4.4, and it reports
missing GNU `realpath -m`, `stat -c`, `find -printf`, and `grep -P` in the Recommended tier.
Each message carries the Homebrew remedy. Developer scripts keep their Bash 4 constructs.

A script that names `/bin/bash` explicitly cannot use the `PATH` interpreter. The one such
script the developer loop runs, the `gdbstub_acl` ufw-prune task under `just test-ansible`,
uses only Bash 3.2 constructs.

## Consequences

macOS contributors run one Homebrew install and one `PATH` edit; a missed step fails at
`just check-deps` with the remedy, not inside a later recipe. Linux hosts pass unchanged:
every supported distribution ships Bash >= 4.4 and the GNU tools. This decision replaces
the direction recorded in the #2647 issue body (port every script to Bash 3.2); the
earlier 3.2-portable guards from #2627 and #2640 stay as they are.

## Considered & rejected

- **Port every developer script to Bash 3.2.** judgment: cost — it rewrites nameref and
  associative-array logic, needs a static guard against regressions that Linux CI (Bash 5)
  cannot catch, and still leaves the GNU flag gaps.
- **Require only Bash, not GNU tools.** verified: `/bin/realpath -m --relative-to=. ./x`
  exits 1 with `illegal option -- m` (macOS, Darwin 27), so `just served-doc-links`
  still fails with Homebrew Bash alone.
- **Floor at Bash 4.3.** judgment: fit — `scripts/live-vm/*` use `shopt -s inherit_errexit`
  (4.4), so a 4.3 floor passes hosts that later fail.
