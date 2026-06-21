# Ansible role tests

Regression harness for the security-critical **`gdbstub_acl` ufw-prune** task (issue #616).

## What it covers

`deploy/ansible/roles/gdbstub_acl/tasks/main.yml` enforces the worker-CIDR ACL on the
raw-TCP gdbstub tier. On Debian/ufw this ACL is the **only** authorization for those ports
(no TLS). The role's **prune** task deletes stale `ALLOW IN` rules on the protected ports
(TLS port + gdbstub range) whose source is not the current `worker_cidr`, by parsing the
human-formatted `ufw status numbered` output and `ufw --force delete`-ing the matching line
numbers, highest-first.

That parse is a hand-rolled `grep`/`sed` pipeline. A regex slip that **under-matches**
silently re-opens the over-permission; one that **over-matches** deletes the current allow
and drops the worker. This harness is the regression net for that pipeline.

## How it works

It drives the **real** prune task — not a copy of the pipeline — in isolation:

- The prune task is tagged `gdbstub_acl_prune`; `run-gdbstub-acl-prune.sh` runs the role with
  `ansible-playbook --tags gdbstub_acl_prune` against `localhost`, so only that task executes
  (the `community.general.ufw` module tasks are sliced out and need no fake).
- A fake `ufw` (`fake-ufw`) is placed on `PATH`. It serves a fixture for `ufw status
  numbered` and appends the rule number to a log for each `ufw --force delete N`. Those are
  the only two ufw calls the prune task makes; anything else makes the fake fail loudly.
- Per case, three signals must all hold:
  1. `ansible-playbook` exits `0`;
  2. the prune task actually ran and reached the pipeline (the fake touched its status
     marker), so an empty delete log is provably a real no-op, not a crash or a tag-skip;
  3. the delete log equals the expected line numbers, in descending order.

Because `--force delete` is the prune's only mutation, asserting the exact delete set proves
the current-CIDR allow, the SSH allow, and the deny rules all survive.

## Running

```sh
just test-ansible
# or directly:
uv run --with 'ansible-core==2.21.1' ./deploy/ansible/tests/run-gdbstub-acl-prune.sh
```

CI runs `just test-ansible` as its own step (`.github/workflows/ci.yml`).

## Fixtures

`fixtures/*.numbered` mirror real `ufw status numbered` output (ufw 0.36.x, Ubuntu 24.04;
each file records this in a header comment, which the prune's grep ignores). Every case uses
`worker_cidr=10.0.0.0/24`, gdbstub range `47000:47099`, TLS port `16514`.

| Fixture | Asserts |
|---------|---------|
| `stale_present` | one stale CIDR on both protected ports → deleted, highest-first; SSH + deny untouched |
| `steady_state` | only current allows + SSH + deny → nothing deleted (no false delete of the current allow) |
| `multiple_stale` | two distinct stale CIDRs → all deleted in strict descending order |
| `broader_mask` | stale `10.0.0.0/16` (not a substring of `10.0.0.0/24`) → deleted |
| `ufw_inactive` | `Status: inactive`, no rules → no-op, no error |
| `non_protected_port` | `ALLOW IN` on `9090/tcp` from a non-worker source → never deleted (port/action scoping) |
| `substring_collision` | stale `110.0.0.0/24` survives — see Known limitation |

### Known limitation (substring exclusion)

The prune excludes the current source with `grep -vF "{{ worker_cidr }}"`, a **substring**
match. A stale allow whose source string *contains* the worker CIDR (e.g. `110.0.0.0/24` vs
`10.0.0.0/24`) is wrongly excluded and **survives** — the "under-match → over-permission
persists" failure #616 names. The `substring_collision` case **pins this current behavior**
(expects no deletion) rather than fixing it: an address-aware exclusion changes the audited
security pipeline and warrants its own review + live re-verification. A future fix must flip
this case's expectation. Tracked as a follow-up to #616.

## Adding a case

1. Add `fixtures/<name>.numbered` (a header comment + real-format `ufw status numbered`).
2. Add a `run_case <name> <name>.numbered <worker_cidr> "<expected descending deletions>"`
   line to `run-gdbstub-acl-prune.sh`.
3. `just test-ansible`.
