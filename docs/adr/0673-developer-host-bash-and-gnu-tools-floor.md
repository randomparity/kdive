# 0673 — Developer hosts supply Bash 4.4 and GNU tools on PATH

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

macOS contributors run one Homebrew install and one `PATH` edit in a login-shell startup
file. A missed Bash step fails at `just check-deps` with the remedy; a missed GNU step is
reported there in the Recommended tier and still fails later in `just ci`. The `gnubin`
entries shadow the BSD `stat`, `find`, `grep`, and other tools in every shell that reads the
file, which can change other BSD-assuming scripts on that host. A Linux host that already
meets the floor (CI runs Bash 5 with GNU tools) sees only the new probes pass; one that does
not gets the same remedy. This decision replaces the direction in the #2647 issue body
(port every script to Bash 3.2); the 3.2-portable guards from #2627 and #2640 stay.

## Considered & rejected

- **Do nothing; document the Homebrew steps only.** judgment: fit — the failures stay deep
  in a run, which is the defect #2647 reports.
- **Prepend the Homebrew paths inside the justfile.** judgment: fit — pytest subprocesses,
  direct script runs, and Ansible `/bin/bash` tasks do not pass through `just`.
- **Port every developer script to Bash 3.2.** judgment: cost — it rewrites nameref and
  associative-array logic, needs a static guard against regressions that Linux CI (Bash 5)
  cannot catch, and still leaves the GNU flag gaps.
- **Require only Bash, not GNU tools.** verified: `/bin/realpath -m --relative-to=. ./x`
  exits 1 with `illegal option -- m` (macOS, Darwin 27), so `just served-doc-links`
  still fails with Homebrew Bash alone.
- **Floor at Bash 4.3.** judgment: fit — `scripts/live-vm/*` use `shopt -s inherit_errexit`
  (4.4), so a 4.3 floor passes hosts that later fail.
