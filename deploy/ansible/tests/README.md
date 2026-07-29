# Ansible role tests

Regression harnesses for role logic that no unit test in `tests/` can reach, all run by
`just test-ansible` (and by CI as its own step):

| Harness | Covers |
|---------|--------|
| `run-gdbstub-acl-prune.sh` | the security-critical `gdbstub_acl` ufw-prune parse (#616) |
| `run-github-runner-preflight.sh` | the `github_runner` host-contract preflight |
| `run-guest-base-image-admission.sh` | the `guest_base_image` build-host admission gate (#1629) |
| `run-remote-libvirt-facts-render.sh` | `remote_libvirt_facts` staged-volume confirmation (#1629) |

Each drives the **real** tasks — never a copy of the logic — in isolation.

## `gdbstub_acl` ufw prune (#616)

### What it covers

`deploy/ansible/roles/gdbstub_acl/tasks/main.yml` enforces the worker-CIDR ACL on the
raw-TCP gdbstub tier. On Debian/ufw this ACL is the **only** authorization for those ports
(no TLS). The role's **prune** task deletes stale `ALLOW IN` rules on the protected ports
(TLS port + gdbstub range) whose source is not the current `worker_cidr`, by parsing the
human-formatted `ufw status numbered` output and `ufw --force delete`-ing the matching line
numbers, highest-first.

That parse is a hand-rolled `grep`/`sed` pipeline. A regex slip that **under-matches**
silently re-opens the over-permission; one that **over-matches** deletes the current allow
and drops the worker. This harness is the regression net for that pipeline.

### How it works

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

### Running

```sh
just test-ansible
# or directly:
uv run --with 'ansible-core==2.21.1' ./deploy/ansible/tests/run-gdbstub-acl-prune.sh
```

CI runs `just test-ansible` as its own step (`.github/workflows/ci.yml`).

### Fixtures

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
| `substring_collision` | stale `110.0.0.0/24` (contains `10.0.0.0/24`) → pruned by exact source-field match (ADR-0201) |
| `prefix_collision` | stale `10.0.0.0/2` (a substring *of* the worker CIDR) → pruned; pins the symmetric direction (ADR-0201) |
| `comment_column` | rules carry a trailing ufw `# comment` → current allow survives, stale pruned; matcher reads the `From` column, not `$NF` (ADR-0201) |

#### Resolved: exact source-field match (ADR-0201)

The prune originally excluded the current source with `grep -vF "{{ worker_cidr }}"`, a
**substring** match, so a stale allow whose source string *contained* the worker CIDR (e.g.
`110.0.0.0/24` vs `10.0.0.0/24`) was wrongly excluded and **survived** — the "under-match →
over-permission persists" failure #616 names. [ADR-0201](../../../docs/adr/0201-gdbstub-acl-prune-exact-source-match.md)
(#648) replaced it with an **exact equality** on the ufw `From` column, read as the field
after the `IN` direction token, so a row matches the current worker iff its source field is
byte-equal to `worker_cidr`. The `substring_collision` case now asserts the stale lines are
deleted, and `prefix_collision`/`comment_column` guard the symmetric substring direction and
the comment-column read (a `$NF` shortcut would delete the commented current allow).

Residual assumption: exact equality requires ufw to render `From` identically to the templated
`worker_cidr`, so supply it as the canonical network CIDR. The harness is a parser/selection
net only; the live re-verification — change `worker_cidr` with a substring-colliding stale
allow present, re-run the role, assert the stale allow is gone **and** the current allow
survives — is the off-CIDR ACL check in [`../README.md`](../README.md).

### Adding a case

1. Add `fixtures/<name>.numbered` (a header comment + real-format `ufw status numbered`).
2. Add a `run_case <name> <name>.numbered <worker_cidr> "<expected descending deletions>"`
   line to `run-gdbstub-acl-prune.sh`.
3. `just test-ansible`.

## Image admission + staged-volume confirmation (#1629)

Two harnesses cover the halves of [ADR-0481](../../../docs/adr/0481-build-host-image-admission-and-staged-volume-confirmation.md).
Both read the **real** catalog from `inventory/group_vars/all.yml` via `vars_files`, so a
catalog change is exercised rather than mirrored into a fixture that can drift.

### `run-guest-base-image-admission.sh`

The admission decision — which of a host's `host_images` this host may build — is a tagged
block in `roles/guest_base_image/tasks/main.yml`, so the harness runs the role with
`--tags guest_base_image_admission` and no build task executes. Per case it passes a distro,
an arch, and a selection as JSON extra-vars, then asserts on the recorded
buildable/skipped name lists.

| Case | Asserts |
|------|---------|
| `rocky_skips_bare` | Rocky cannot produce `bare-kdive-remote-base` (no busybox) → skipped, and the rocky image still builds |
| `fedora_builds_bare` | Fedora ships busybox → both build; the gate must not over-restrict |
| `ubuntu_builds_bare` | Debian-family ships busybox → both build |
| `unconstrained_entry_anywhere` | an entry with no `host_distros` is admitted on any distro |
| `rocky_bare_default` | an unbuildable `host_default_image` fails fast — a skip would leave the host with no usable `base_image` |
| `arch_mismatch_still_fails` | `arches` stays a hard failure, not a skip (ADR-0481 decision 3) |

A passing case additionally requires the gate's own `TASK [...]` header in the log, so a
matching decision is provably an evaluation and not a tag slice that skipped the role. A
failing case additionally requires the expected message, so the non-zero exit is the intended
assert rather than an unrelated error.

### `run-remote-libvirt-facts-render.sh`

`kind = "staged"` claims the volume is already in the host's pool. The fixture here is just
which `.qcow2` files exist, so the harness needs no fake binary: it points
`storage_pool_target` at a temp dir, `touch`es some volumes, runs the real role, and asserts
on the rendered artifact.

| Case | Asserts |
|------|---------|
| `both_staged` | both volumes present → both declared, no markers |
| `bare_absent` | the #1629 shape — the skipped bare image is **not** declared, the rocky image still is, `# OMITTED` records the gap |
| `default_absent` | the default image's volume missing → `# INCOMPLETE`, so the fragment is rejected at load rather than at provision |
| `fresh_host` | nothing staged (site.yml before image.yml) → no `[[image]]` at all, and the render still exits 0 |

The `[[image]]` declarations are read back out of the rendered TOML, so a template that
re-widened its loop to the whole selection fails three of the four cases.
