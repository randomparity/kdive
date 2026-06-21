# ADR 0200 — Hermetic regression harness for the gdbstub_acl ufw prune

- **Status:** Accepted
- **Date:** 2026-06-21
- **Deciders:** KDIVE maintainers

## Context

`deploy/ansible/roles/gdbstub_acl/tasks/main.yml` enforces the worker-CIDR ACL on the
raw-TCP gdbstub tier. On the ufw (Debian) path this ACL is the **only** authorization for
those ports — the gdbstub range carries no TLS, so a wrong rule means unauthenticated
full-VM memory access.

The role gained a **prune step** (commit `782f3f65`, hardened in `6310f844`) that deletes
`ALLOW IN` rules on the protected ports whose source is not the current `worker_cidr`, to
clean up stale allows after a `worker_cidr` change. It is a hand-rolled shell pipeline that
greps/seds the human-formatted `ufw status numbered` output to pick line numbers, then
`ufw --force delete`s them highest-first (so renumbering does not invalidate later targets).

This security-critical step has **no automated coverage** (#616). `just lint-ansible` is
syntax/style only; the sole verification to date is a single manual live run. The pipeline
is brittle to:

- ufw output-format drift (column padding, a future `(v6)`/comment column, locale changes),
- a regex slip in the templated `gdbstub_acl_tls_port` / `gdbstub_range` values.

A future edit that makes the port pattern **under-match** silently re-opens the
over-permission; one that makes the `grep -vF "{{ worker_cidr }}"` exclusion **over-match**
deletes the current allow and drops the worker mid-run. Nothing in CI would catch either.

The risk lives entirely in **text parsing** of `ufw status numbered`, not in live network
state — which means it can be exercised hermetically, without a real firewall.

## Decision

Add a hermetic regression harness that drives the **real** prune task — not a copy of its
pipeline — against canned `ufw status numbered` fixtures, and assert on the exact set and
order of rules it deletes.

- Tag the prune task `gdbstub_acl_prune`. The harness runs the role with
  `--tags gdbstub_acl_prune` so only that task executes; the `community.general.ufw` module
  tasks (allow/deny/enable) are sliced out and need no fake. The block-level
  `when: ansible_os_family == 'Debian'` is satisfied with a play var under
  `gather_facts: false`.
- A fake `ufw` shim on `PATH` makes the task hermetic: `ufw status numbered` prints a
  fixture file (`$FAKE_UFW_FIXTURE`); `ufw --force delete N` appends `N` to a delete log
  (`$FAKE_UFW_DELETE_LOG`). These are the only two ufw invocations the prune task makes.
- The prune task's **only** mutating ufw call is `--force delete`, so "the current-CIDR
  allow, the SSH allow, and the deny rules survive" is provable by asserting the delete log
  equals *exactly* the expected stale line numbers, in descending order — no surviving-state
  model needed.
- A bash runner (`deploy/ansible/tests/run-gdbstub-acl-prune.sh`) loops fixture cases,
  invokes `ansible-playbook` once per case with a fresh delete log, and diffs the log
  against each case's expected deletions. A new `just test-ansible` recipe runs it, gated
  explicitly in `ci.yml` (CI invokes recipes individually).

Fixture cases pin the failure modes #616 names: stale-present (delete exactly the stale
allows), steady-state (no stale → delete nothing, pipefail-safe), multiple distinct stale
CIDRs (highest-first ordering), a stale broader-mask CIDR sharing the current network prefix
(exclusion is on the full CIDR string, not a prefix), ufw inactive (no rules → no-op), and a
non-protected-port allow plus deny/SSH rows present (port- and action-scoping).

## Consequences

- The repo gains its first ansible role test infrastructure, scoped to one role and built
  only from bash + the already-pinned `ansible-core` (`uv run --with`). No Docker, no
  privileged container, no molecule, no new linted-dependency surface.
- A future edit that breaks the parse (under- or over-match), the port/action scoping, or
  the highest-first ordering fails CI deterministically. The harness exercises the live
  task, so role and test cannot drift.
- The harness does **not** validate real ufw rule application or real netfilter behavior —
  it is a parser/selection regression net, not an integration test. Live application stays
  covered only by the operator runbook's manual run, as before. New ufw output-format drift
  is caught only once a fixture is added for it.
- The fake `ufw` and runner are linted (shellcheck/shfmt via `lint-shell`); the test
  playbook is linted by `lint-ansible` (production profile).

## Considered & rejected

- **Molecule + a privileged Debian container running real ufw** (#616's first-choice
  option). Highest fidelity, but needs a privileged container, molecule + a driver
  dependency, and working netfilter inside CI — a large new dependency and gating surface
  for a risk that is purely text parsing. Rejected as disproportionate; the issue explicitly
  permits the lighter harness ("or, at minimum, a check-mode + assertion harness").
- **Extracting the pipeline into a `files/` script and unit-testing the script.** Cleanest
  to test, but changes the security-critical role's implementation rather than only adding a
  test, and an extracted script would still need the same fixture-driven coverage. Rejected
  to keep the role's audited inline pipeline untouched (the harness adds only a tag).
- **Re-implementing the grep/sed pipeline inside the test and asserting on that.** A future
  edit to the role would not be caught — test and role drift apart. Rejected; the harness
  drives the real task.
- **Asserting on surviving rules instead of deletions.** The fake would have to model ufw's
  renumber-on-delete state to report a post-prune `status`. Unnecessary: deletions are the
  only mutation, so the delete log fully determines what survived.
