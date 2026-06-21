# gdbstub_acl ufw-prune regression harness

- **Issue:** #616
- **ADR:** [ADR-0200](../adr/0200-gdbstub-acl-prune-regression-harness.md)
- **Date:** 2026-06-21
- **Status:** Implemented

## Problem

`deploy/ansible/roles/gdbstub_acl/tasks/main.yml` enforces the worker-CIDR ACL on the
raw-TCP gdbstub tier. On Debian/ufw this ACL is the **only** authorization for those ports
(no TLS), so a wrong rule means unauthenticated full-VM memory access.

The role's **prune** task deletes `ALLOW IN` rules on the protected ports (TLS port +
gdbstub range) whose source is not the current `worker_cidr`, to clean up stale allows after
a `worker_cidr` change. It is a hand-rolled shell pipeline over the human-formatted
`ufw status numbered` output:

```
ufw status numbered \
  | grep -E "\] +(<tls_port>|<gdbstub_range>)/tcp +ALLOW IN " \
  | grep -vF "<worker_cidr>" \
  | sed -E 's/^\[ *([0-9]+)\].*/\1/' | sort -rn
```

then `ufw --force delete` each line number, highest-first.

This step has no automated coverage. It is brittle to ufw output-format drift and to a regex
slip in the templated port values. Two silent failure modes:

1. **Under-match** (port pattern too narrow) → a stale allow is *not* pruned → the
   over-permission persists.
2. **Over-match** (the `grep -vF` exclusion misses) → the *current* allow is deleted → the
   worker is dropped mid-run.

Nothing in CI catches either.

## Goal

A deterministic, CI-gated regression net that exercises the **real** prune task against
canned `ufw status numbered` inputs and asserts exactly which rules it deletes — without a
real firewall, root, Docker, or molecule.

## Approach (per ADR-0200)

Drive the live task hermetically:

1. **Tag** the prune task `gdbstub_acl_prune`. The harness runs the role with
   `--tags gdbstub_acl_prune`, so only that task executes; the `community.general.ufw`
   module tasks (allow/deny/enable/reload, and the active-state assert) are sliced out and
   need no fake. The block `when: ansible_os_family == 'Debian'` is satisfied with a play
   var, `gather_facts: false`.
2. **Fake `ufw`** on `PATH`:
   - `ufw status numbered` → `cat "$FAKE_UFW_FIXTURE"`,
   - `ufw --force delete N` → `echo N >> "$FAKE_UFW_DELETE_LOG"`,
   - any other invocation → fail loudly (the prune task must make no other ufw call).
3. **Assert** the delete log equals the case's expected stale line numbers, in descending
   order. Because `--force delete` is the prune's only mutation, this single assertion
   proves the current-CIDR allow, the SSH allow, and the deny rules all survive.

### Components

| Path | Role |
|------|------|
| `deploy/ansible/roles/gdbstub_acl/tasks/main.yml` | add `tags: [gdbstub_acl_prune]` to the prune task (only role change) |
| `deploy/ansible/tests/gdbstub_acl_prune.yml` | minimal play that applies the role (`connection: local`, `gather_facts: false`, vars set per invocation) |
| `deploy/ansible/tests/fixtures/*.numbered` | canned `ufw status numbered` outputs, one per case |
| `deploy/ansible/tests/fake-ufw` | the fake `ufw` shim (shellcheck/shfmt-clean) |
| `deploy/ansible/tests/run-gdbstub-acl-prune.sh` | runner: per case, set env + PATH, run `ansible-playbook`, diff the delete log against expected |
| `deploy/ansible/tests/README.md` | how the harness works / how to add a case |
| `justfile` | `test-ansible` recipe; `lint-shell` extended to cover `deploy/ansible/tests` |
| `.github/workflows/ci.yml` | explicit `just test-ansible` step (CI runs recipes individually) |

### Fixture cases (acceptance)

Each case is `(fixture, worker_cidr, gdbstub_range, expected-deletions-descending)`. The
harness fails if the observed delete log differs (set or order).

1. **stale-present** — current allows + one stale-CIDR allow on both protected ports →
   delete exactly the two stale lines, highest-first. SSH + deny rows present and untouched.
2. **steady-state** — only current allows + SSH + deny, no stale → delete nothing
   (pipefail-safe no-op; the steady state must not error or delete the current allow).
3. **multiple-stale** — two distinct stale CIDRs across the protected ports → delete all
   stale lines in strict descending order (pins highest-first against renumber hazard).
4. **broader-mask-same-prefix** — current `10.0.0.0/24`, a stale `10.0.0.0/16` allow →
   the stale broader-mask allow is pruned (the `grep -vF` exclusion is on the full CIDR
   string, not a prefix; a different mask is a different, broader source).
5. **ufw-inactive** — `Status: inactive`, no numbered rules → delete nothing, no error
   (the role claims safe-when-inactive).
6. **non-protected-port** — an `ALLOW IN` on a non-protected port (e.g. `9090/tcp`) from a
   non-worker source, plus the protected deny rows → never deleted (port- and
   action-scoping: only `ALLOW IN` on exactly the TLS port or gdbstub range can match).

## Non-goals

- Validating real ufw rule application or netfilter behavior — that stays the operator
  runbook's manual live run. This is a parser/selection regression net.
- Testing the firewalld (RedHat) path, which uses the `firewalld` module with assertions
  already inline, not a text-parse pipeline.
- A general molecule framework for all roles (out of scope; see ADR-0200 rejected options).

## Limitations

- New ufw output-format drift is caught only once a fixture reproducing it is added.
- The fake `ufw` models only the two calls the prune task makes; a future prune edit that
  shells a third ufw subcommand will make the fake fail loudly (intended — surfaces the
  change), and the harness must then be taught that call.
