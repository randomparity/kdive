# Developer host Bash and GNU tools floor

Issue: #2647. Decision: [ADR-0672](../../adr/0672-developer-host-bash-and-gnu-tools-floor.md).

## Problem

On stock macOS (`bash` 3.2), four developer recipes fail deep in a run. The operator chose
a Homebrew Bash and GNU tools floor. Each change below extends its current owner.

## Scope

- `scripts/check-setup-deps.sh` owns the floor. A Bash-3.2-safe block after argument parsing
  reads the running `BASH_VERSINFO` and runs the `bash` on `PATH` for its version. When
  either is older than 4.4, or `PATH` has no `bash`, it prints the found version and path
  and one remedy (distribution package, or `brew install bash` with
  `"$(brew --prefix)/bin"` before `/bin` on `PATH`), then exits 1 before any probe.
- `probe_all` adds four Recommended-tier `note_manual` probes: `realpath -m --relative-to`
  and `stat -c` (coreutils), `find -printf` (findutils), `grep -P` (grep). Each hint names
  the distribution package, the Homebrew formula, and its `opt/<formula>/libexec/gnubin`
  `PATH` entry. Hints do not branch on the host OS.
- `gdbstub_acl` prune task: replace `mapfile` with
  `while IFS= read -r n; do ...; done < <(pipeline)`. The pipeline, its `|| true`
  wrappers, and `sort -rn` stay; `ufw --force delete` reads `/dev/null`.
- Docs: `docs/operating/install.md` gains a macOS block; `CONTRIBUTING.md` and `AGENTS.md`
  state the floor with a link.
- `test_check_setup_deps.py` moves to `requires_bash(4, 4, ...)`; its runners append a
  directory that holds only the real `bash` to `PATH`.

### Failure model

- Actors and deployments: a developer or CI job runs `check-deps` on Linux or macOS;
  Ansible runs the prune task as root on worker hosts and in the local harness.
- Invariants: the prune deletes only stale rules, highest number first; a host that meets
  the floor keeps its current exit status.
- Accepted: a non-GNU tool that accepts the probed flags passes (the recipes need only
  those flags); the probe reads only the first `bash` on `PATH`.
- Covered elsewhere: `scripts/live-stack/*` and the `completion.py` script stay unchanged
  (ADR-0672 exclusions).

## Success

1. A running interpreter or `PATH` `bash` older than 4.4 makes `check-deps` exit 1 with the
   Homebrew remedy before any tier report.
2. A missing GNU feature appears in the Recommended tier with its formula and `gnubin`
   step; the exit status stays as the Required tier sets it.
3. The three docs state Bash >= 4.4 and the macOS steps.
4. The prune task runs under `/bin/bash` 3.2 and deletes the same rules in the same order.
5. A host that meets the floor and has GNU tools gets the same output as before.

## Validation

- S1 `PATH` bash — focused-test: a stub `bash` that reports 3.2 first on `PATH`; exit 1
  and the remedy.
- S1 interpreter — focused-test: run under `/bin/bash` if older than 4.4, else skip.
- S2 — focused-test: empty `PATH` lists the four features and `gnubin` hints; stubs that
  accept the flags clear them.
- S3 — task-test-not-applicable: prose for humans.
- S4 — focused-test: `deploy/ansible/tests/run-gdbstub-acl-prune.sh` on macOS `/bin/bash`
  3.2 and in Linux CI.
- S5 — focused-test: the existing 51 checker tests pass with the `bash` directory added.
