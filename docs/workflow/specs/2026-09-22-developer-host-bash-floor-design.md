# Developer host Bash and GNU tools floor

Issue: #2647. Decision: [ADR-0672](../../adr/0672-developer-host-bash-and-gnu-tools-floor.md).

## Problem

On stock macOS (`bash` 3.2), four developer recipes fail deep in a run. Each change below
extends its current owner.

## Scope

- `scripts/check-setup-deps.sh` owns the floor. After argument parsing, one Bash-3.2-safe
  function compares a `major.minor` with 4.4 and reports. It gets the running
  `BASH_VERSINFO`, then the `PATH` `bash` via builtins only
  (`bash -c 'printf %s.%s "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'`). No `bash`, a failed
  query, or unparsable output counts as too old. It prints the version and path, the
  distribution remedy, and `brew install bash` with `"$(brew --prefix)/bin"` before `/bin`
  on `PATH`, then exits 1 before any probe.
- `probe_all` adds four Recommended-tier `note_manual` probes: `realpath -m --relative-to`
  and `stat -c` (coreutils), `find -printf` (findutils), `grep -P` (grep). Each hint names
  the Homebrew formula and its `opt/<formula>/libexec/gnubin` `PATH` entry. No remedy
  branches on the host OS.
- `gdbstub_acl` prune task: replace `mapfile` with
  `while IFS= read -r n; do ...; done < <(pipeline)`. The pipeline, its `|| true`
  wrappers, and `sort -rn` stay; `ufw --force delete` reads `/dev/null`.
- Docs: `install.md` gains a macOS block that sets `PATH` in `~/.zprofile` and says GUI
  git clients do not read it; `CONTRIBUTING.md` and `AGENTS.md` link it.
- Checker tests move to `requires_bash(4, 4, ...)` and get the real `bash` on `PATH`.

### Failure model

- Actors and deployments: a developer or CI job runs `check-deps` on Linux or macOS;
  Ansible runs the prune task as root on worker hosts and in the local harness.
- Invariants: the prune deletes only stale rules, highest number first; a host that meets
  the floor keeps its current exit status.
- Accepted: a non-GNU tool that accepts the probed flags passes; only the first `bash`
  on `PATH` is read.
- Covered elsewhere: `scripts/live-stack/*` and `completion.py` (#2647 exclusions).

## Success

1. An interpreter or `PATH` `bash` older than 4.4 makes `check-deps` exit 1 with the
   remedy before any tier report.
2. A missing GNU feature appears in the Recommended tier with its formula and `gnubin`
   step; the exit status stays as the Required tier sets it.
3. `install.md` states Bash >= 4.4 and the macOS steps; the other two docs link it.
4. The prune task runs under `/bin/bash` 3.2 and deletes the same rules in the same order.
5. A host that meets the floor and has GNU tools gets the same output as before.

## Validation

- S1 — focused-test: stub `bash` reporting 3.2, and a failing stub, first on `PATH`;
  exit 1 and the remedy. The shared function makes this CI coverage; a `/bin/bash`
  run covers the interpreter read where it is older than 4.4, else skips.
- S2 — focused-test: a `PATH` of only `bash` lists the four features and `gnubin` hints; stubs that
  accept the flags clear them.
- S3 — task-test-not-applicable: human prose.
- S4 — focused-test: `run-gdbstub-acl-prune.sh` on macOS `/bin/bash` 3.2 and Linux CI.
- S5 — focused-test: the 51 existing checker tests pass.
