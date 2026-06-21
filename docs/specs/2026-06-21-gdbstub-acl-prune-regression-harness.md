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
   - `ufw status numbered` → touch `$FAKE_UFW_STATUS_MARKER`, then `cat "$FAKE_UFW_FIXTURE"`,
   - `ufw --force delete N` → `echo N >> "$FAKE_UFW_DELETE_LOG"`,
   - any other invocation → fail loudly (the prune task must make no other ufw call).
3. **Assert** the delete log equals the case's expected stale line numbers, in descending
   order. Because `--force delete` is the prune's only mutation, this single assertion
   proves the current-CIDR allow, the SSH allow, and the deny rules all survive the prune.

### Verification contract (so a no-op case can't false-pass)

A bare delete-log diff is insufficient: an empty log is also what an **errored** prune task
(a `set -euo pipefail` regression, a broken `sed`) or a **skipped** task (a tag typo, so
`--tags` matches nothing) produces — which would silently pass the zero-deletion cases
(steady-state, ufw-inactive). The runner therefore requires three signals per case, all of
which must hold:

1. `ansible-playbook` exits `0`.
2. The prune task **actually executed and reached the pipeline** — proven by the fake `ufw`
   touching a `$FAKE_UFW_STATUS_MARKER` file when it serves `status numbered`. The runner
   asserts the marker exists for every case (an empty delete log is then provably a real
   no-op, not a crash or a skip).
3. The delete log equals the expected line numbers, in the expected (descending) order.

The runner sets `ANSIBLE_PYTHON_INTERPRETER` so the per-run interpreter-discovery warning
does not pollute output (zero-warnings policy).

### Components

| Path | Role |
|------|------|
| `deploy/ansible/roles/gdbstub_acl/tasks/main.yml` | add `tags: [gdbstub_acl_prune]` to the prune task (only role change) |
| `deploy/ansible/tests/gdbstub_acl_prune.yml` | minimal play that applies the role (`connection: local`, `gather_facts: false`, vars set per invocation) |
| `deploy/ansible/tests/fixtures/*.numbered` | canned `ufw status numbered` outputs, one per case, mirroring real ufw output (each carries a comment header citing the ufw version the format represents) |
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
7. **substring-collision** — current `10.0.0.0/24`, a stale allow from `110.0.0.0/24` (a
   real routable range whose string *contains* the worker CIDR). Originally pinned the
   substring-exclusion bug (the stale allow survived). Since [ADR-0201](../adr/0201-gdbstub-acl-prune-exact-source-match.md)
   (#648) replaced the substring filter with an exact source-field match, this case now
   asserts the stale lines are **deleted**, highest-first — the matching now keys on the
   `From` column, so a substring collision is no longer mistaken for the current source.
8. **prefix-collision** (ADR-0201) — current `10.0.0.0/24`, a stale allow from `10.0.0.0/2`
   (a source string that is a substring *of* the worker CIDR). The exact-equality matcher
   prunes it; pins the symmetric substring direction.
9. **comment-column** (ADR-0201) — protected-port `ALLOW IN` rows carry a trailing ufw
   `# comment`. The current `10.0.0.0/24` allow (with a comment) survives and the stale
   `192.168.99.0/24` allow (with a comment) is pruned, proving the matcher reads the source
   column (the field after `IN`), not the last whitespace token (`$NF`), which a comment
   would shift off the source.

### Negative / mutation check (acceptance gate)

Per the repo's "verify tests catch failures" standard, the implementer must demonstrate the
harness goes **red** when the prune parse is deliberately broken — recorded in the PR body,
not committed:

- Narrow the port pattern (drop the TLS port) → the **stale-present** case must fail (stale
  no longer pruned).
- Break the exclusion (`grep -vF` of a non-matching literal) → the **steady-state** case
  must fail (the current allow gets spuriously deleted).

A harness that stays green under both mutations is inert and does not satisfy this spec.

## Non-goals

- Validating real ufw rule application or netfilter behavior — that stays the operator
  runbook's manual live run. This is a parser/selection regression net.
- Testing the firewalld (RedHat) path, which uses the `firewalld` module with assertions
  already inline, not a text-parse pipeline.
- A general molecule framework for all roles (out of scope; see ADR-0200 rejected options).

## Limitations

- New ufw output-format drift is caught only once a fixture reproducing it is added. The
  fixtures mirror real `ufw status numbered` output and record the ufw version they
  represent, but they are static — they cannot anticipate a future ufw format change.
- The fake `ufw` models only the two calls the prune task makes; a future prune edit that
  shells a third ufw subcommand will make the fake fail loudly (intended — surfaces the
  change), and the harness must then be taught that call.
- ~~**Known role weakness (substring exclusion, case 7):** the prune excludes the current
  source with `grep -vF "{{ worker_cidr }}"`, a substring match, so a stale allow whose
  source string *contains* the worker CIDR (e.g. `110.0.0.0/24` vs `10.0.0.0/24`) is wrongly
  excluded and **survives** — exactly the "under-match → over-permission persists" failure
  #616 names.~~ *Resolved by [ADR-0201](../adr/0201-gdbstub-acl-prune-exact-source-match.md)
  (#648): the exclusion is now an exact equality on the ufw `From` column, read as the field
  after the `IN` direction token, so a substring-colliding stale allow is pruned. The
  `substring_collision` case now expects the stale lines deleted, and two regression fixtures
  were added — `prefix_collision` (a source that is a substring *of* the worker CIDR, pruned)
  and `comment_column` (a trailing ufw comment does not shift the matched source off the
  current allow). Because exact equality assumes ufw renders the `From` column identically to
  the templated `worker_cidr`, ADR-0201's live re-verification (the off-CIDR ACL refusal check
  in `../../deploy/ansible/README.md`) asserts both that the stale collision is pruned and that
  the current allow survives.*
