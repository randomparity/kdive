# gdbstub_acl ufw-prune regression harness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) to
> implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a hermetic, CI-gated regression harness that drives the real `gdbstub_acl`
ufw-prune task against canned `ufw status numbered` fixtures and asserts exactly which rules
it deletes.

**Architecture:** Tag the prune task; run it in isolation with `ansible-playbook --tags`
against `localhost`; a fake `ufw` on `PATH` serves fixtures for `status numbered` and logs
`--force delete N`. A bash runner loops cases, asserting (1) playbook exit 0, (2) the prune
task ran (fake touched a marker on `status numbered`), (3) the delete log equals the expected
descending line numbers.

**Tech Stack:** bash, `ansible-core==2.21.1` (via `uv run --with`), shellcheck/shfmt,
`just`, GitHub Actions.

**Spec:** `docs/specs/2026-06-21-gdbstub-acl-prune-regression-harness.md`
**ADR:** `docs/adr/0200-gdbstub-acl-prune-regression-harness.md`

## Global Constraints

- `lint-shell` = `shfmt -f <dirs> | xargs shellcheck` + `shfmt -i 2 -d <dirs>`; all shell
  must pass shellcheck and be 2-space-indented. Every shell file: `set -euo pipefail`.
- `lint-ansible` runs yamllint + ansible-lint (profile: `production`) over `deploy/ansible`;
  the test playbook must pass. Avoid setting `ansible_*` reserved vars in YAML (var-naming) —
  pass `ansible_os_family=Debian` via `-e` from the runner instead.
- CI invokes recipes **individually** (never `just ci`); a new gate needs an explicit
  `ci.yml` step.
- The only role change is adding a tag to the prune task. Do not alter the prune pipeline.
- `ansible-core` is pinned `2.21.1` (matches `lint-ansible`).

---

### Task 1: Tag the prune task

**Files:**
- Modify: `deploy/ansible/roles/gdbstub_acl/tasks/main.yml` (prune task — add `tags`)

Already applied in the working tree:

```yaml
      register: gdbstub_acl_ufw_prune
      changed_when: "'pruned=1' in gdbstub_acl_ufw_prune.stdout"
      # Tagged so the regression harness (deploy/ansible/tests/) can drive this
      # security-critical parse-and-delete pipeline in isolation with a fake ufw.
      tags:
        - gdbstub_acl_prune
```

- [ ] **Step 1:** Confirm the tag is present and `lint-ansible` still passes after the other
  files exist (linted together in Task 6).

---

### Task 2: Fake `ufw` shim

**Files:**
- Create: `deploy/ansible/tests/fake-ufw`

**Interfaces:**
- Reads env: `FAKE_UFW_FIXTURE` (file to cat for `status numbered`),
  `FAKE_UFW_DELETE_LOG` (append `N` per `--force delete N`),
  `FAKE_UFW_STATUS_MARKER` (touch on `status numbered`).
- Produces: stdout = fixture content; side effects = marker touched, delete log appended.

- [ ] **Step 1:** Write the shim:

```bash
#!/usr/bin/env bash
# Test double for `ufw`, used only by the gdbstub_acl prune regression harness.
# Handles exactly the two subcommands the prune task issues; anything else is a
# loud failure (the prune task must make no other ufw call).
set -euo pipefail

case "${1:-} ${2:-}" in
  "status numbered")
    touch "$FAKE_UFW_STATUS_MARKER"
    cat "$FAKE_UFW_FIXTURE"
    ;;
  "--force delete")
    printf '%s\n' "${3:?fake-ufw: 'delete' needs a rule number}" >>"$FAKE_UFW_DELETE_LOG"
    ;;
  *)
    echo "fake-ufw: unexpected invocation: $*" >&2
    exit 64
    ;;
esac
```

- [ ] **Step 2:** `chmod +x deploy/ansible/tests/fake-ufw`.
- [ ] **Step 3:** `shellcheck deploy/ansible/tests/fake-ufw && shfmt -i 2 -d deploy/ansible/tests/fake-ufw` → clean.

---

### Task 3: Test playbook

**Files:**
- Create: `deploy/ansible/tests/gdbstub_acl_prune.yml`

**Interfaces:**
- Consumes (via runner `-e`): `worker_cidr`, `gdbstub_range`, `gdbstub_acl_tls_port`,
  `ansible_os_family`.
- Produces: applies the `gdbstub_acl` role; under `--tags gdbstub_acl_prune` only the prune
  task runs.

- [ ] **Step 1:** Write the minimal play (no vars — all supplied via `-e` to keep
  ansible-lint clean):

```yaml
---
- name: Exercise the gdbstub_acl prune task in isolation (regression harness)
  hosts: localhost
  connection: local
  gather_facts: false
  roles:
    - role: gdbstub_acl
```

- [ ] **Step 2:** yamllint/ansible-lint validated in Task 6.

---

### Task 4: Fixtures

**Files:**
- Create: `deploy/ansible/tests/fixtures/stale_present.numbered`
- Create: `deploy/ansible/tests/fixtures/steady_state.numbered`
- Create: `deploy/ansible/tests/fixtures/multiple_stale.numbered`
- Create: `deploy/ansible/tests/fixtures/broader_mask.numbered`
- Create: `deploy/ansible/tests/fixtures/ufw_inactive.numbered`
- Create: `deploy/ansible/tests/fixtures/non_protected_port.numbered`
- Create: `deploy/ansible/tests/fixtures/substring_collision.numbered`

Each fixture begins with a one-line `#` provenance/version comment (inert: it never matches
the prune's port/action grep), then mirrors real `ufw status numbered` output. Worker CIDR is
`10.0.0.0/24`, gdbstub range `47000:47099`, TLS port `16514` for every case.

- [ ] **Step 1:** `stale_present.numbered` — stale `192.168.99.0/24` on lines 6,7.
  Expected deletions: `7 6`.
- [ ] **Step 2:** `steady_state.numbered` — only current allows + SSH + deny. Expected: empty.
- [ ] **Step 3:** `multiple_stale.numbered` — stale `192.168.99.0/24` (6,7) and
  `172.16.5.0/24` (8,9). Expected: `9 8 7 6`.
- [ ] **Step 4:** `broader_mask.numbered` — stale `10.0.0.0/16` (6,7); not a substring of
  `10.0.0.0/24`, so pruned. Expected: `7 6`.
- [ ] **Step 5:** `ufw_inactive.numbered` — `Status: inactive`, no rules. Expected: empty.
- [ ] **Step 6:** `non_protected_port.numbered` — an `ALLOW IN 9090/tcp` from a non-worker
  source (line 6); not a protected port. Expected: empty.
- [ ] **Step 7:** `substring_collision.numbered` — stale `110.0.0.0/24` (6,7) which contains
  `10.0.0.0/24` as a substring → wrongly excluded by `grep -vF` → survives. Expected: empty
  (pins the known role weakness; see spec Limitations).

Exact fixture content is in Task 5's verification (the runner's case table encodes the
expected deletions). Example (`stale_present.numbered`):

```
# Fixture: stale-present. Mirrors `ufw status numbered` (ufw 0.36.x, Ubuntu 24.04).
Status: active

     To                         Action      From
     --                         ------      ----
[ 1] 22/tcp                     ALLOW IN    Anywhere
[ 2] 16514/tcp                  ALLOW IN    10.0.0.0/24
[ 3] 47000:47099/tcp            ALLOW IN    10.0.0.0/24
[ 4] 16514/tcp                  DENY IN     Anywhere
[ 5] 47000:47099/tcp            DENY IN     Anywhere
[ 6] 16514/tcp                  ALLOW IN    192.168.99.0/24
[ 7] 47000:47099/tcp            ALLOW IN    192.168.99.0/24
```

---

### Task 5: Runner

**Files:**
- Create: `deploy/ansible/tests/run-gdbstub-acl-prune.sh`

**Interfaces:**
- Consumes: fixtures + playbook + the `gdbstub_acl` role.
- Produces: exit 0 iff every case passes all three signals; per-case `ok`/`FAIL` lines.

- [ ] **Step 1:** Write the runner (case table encodes expected deletions, descending):

```bash
#!/usr/bin/env bash
# Regression harness for the gdbstub_acl ufw-prune task (issue #616).
# Drives the REAL prune task in isolation against canned `ufw status numbered`
# fixtures with a fake `ufw`, asserting exactly which rules it deletes.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/../../.." && pwd)"
playbook="$here/gdbstub_acl_prune.yml"
fixtures="$here/fixtures"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
install -m 0755 "$here/fake-ufw" "$work/ufw"

export PATH="$work:$PATH"
export ANSIBLE_ROLES_PATH="$repo_root/deploy/ansible/roles"
export ANSIBLE_PYTHON_INTERPRETER="${ANSIBLE_PYTHON_INTERPRETER:-$(command -v python3)}"
export ANSIBLE_NOCOWS=1
export ANSIBLE_LOCALHOST_WARNING=False
export ANSIBLE_INVENTORY_UNPARSED_WARNING=False

fail=0

run_case() {
  local name="$1" fixture="$2" worker_cidr="$3" expected="$4"
  local dir="$work/$name"
  mkdir -p "$dir"
  export FAKE_UFW_FIXTURE="$fixtures/$fixture"
  export FAKE_UFW_DELETE_LOG="$dir/deletes.log"
  export FAKE_UFW_STATUS_MARKER="$dir/status.marker"
  : >"$FAKE_UFW_DELETE_LOG"
  rm -f "$FAKE_UFW_STATUS_MARKER"

  local rc=0
  ansible-playbook "$playbook" -i localhost, \
    --tags gdbstub_acl_prune \
    -e ansible_os_family=Debian \
    -e "worker_cidr=$worker_cidr" \
    -e gdbstub_range=47000:47099 \
    -e gdbstub_acl_tls_port=16514 \
    >"$dir/out.log" 2>&1 || rc=$?

  if [ "$rc" -ne 0 ]; then
    echo "FAIL [$name]: ansible-playbook exited $rc"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi
  if [ ! -f "$FAKE_UFW_STATUS_MARKER" ]; then
    echo "FAIL [$name]: prune task never queried 'ufw status numbered' (skipped or wedged)"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi
  local actual
  actual="$(tr '\n' ' ' <"$FAKE_UFW_DELETE_LOG" | sed 's/  */ /g; s/^ //; s/ $//')"
  if [ "$actual" != "$expected" ]; then
    echo "FAIL [$name]: deleted [$actual], expected [$expected]"
    fail=1
    return 0
  fi
  echo "ok   [$name]: deleted [$actual]"
  return 0
}

#         name                  fixture                       worker_cidr   expected(desc)
run_case  stale_present         stale_present.numbered        10.0.0.0/24   "7 6"
run_case  steady_state          steady_state.numbered         10.0.0.0/24   ""
run_case  multiple_stale        multiple_stale.numbered       10.0.0.0/24   "9 8 7 6"
run_case  broader_mask          broader_mask.numbered         10.0.0.0/24   "7 6"
run_case  ufw_inactive          ufw_inactive.numbered         10.0.0.0/24   ""
run_case  non_protected_port    non_protected_port.numbered   10.0.0.0/24   ""
# Known role weakness (substring grep -vF): 110.0.0.0/24 contains 10.0.0.0/24, so it is
# wrongly excluded and SURVIVES. Pinned as current behavior — see spec Limitations.
run_case  substring_collision   substring_collision.numbered  10.0.0.0/24   ""

if [ "$fail" -ne 0 ]; then
  echo "gdbstub_acl prune harness: FAILED"
  exit 1
fi
echo "gdbstub_acl prune harness: all cases passed"
```

- [ ] **Step 2:** `chmod +x`. `shellcheck` + `shfmt -i 2 -d` → clean.
- [ ] **Step 3:** Run `uv run --with 'ansible-core==2.21.1' ./deploy/ansible/tests/run-gdbstub-acl-prune.sh`.
  Expected: all 7 cases `ok`, exit 0.

---

### Task 6: Wire into just + CI; README

**Files:**
- Modify: `justfile` (add `test-ansible`; extend `lint-shell` to cover `deploy/ansible/tests`)
- Modify: `.github/workflows/ci.yml` (add a `just test-ansible` step)
- Create: `deploy/ansible/tests/README.md`

- [ ] **Step 1:** `justfile` — extend `lint-shell` targets and add the recipe:

```
lint-shell:
    shfmt -f scripts deploy/remote-libvirt-guest-helpers deploy/ansible/tests | xargs shellcheck
    shfmt -i 2 -d scripts deploy/remote-libvirt-guest-helpers deploy/ansible/tests
```

```
# Run the Ansible role regression harness (gdbstub_acl ufw prune, #616).
test-ansible:
    uv run --with 'ansible-core==2.21.1' ./deploy/ansible/tests/run-gdbstub-acl-prune.sh
```

  Add `test-ansible` to the aggregate `ci` recipe too (after `lint-ansible`).

- [ ] **Step 2:** `.github/workflows/ci.yml` — after the "Lint Ansible" step:

```yaml
      - name: Ansible role tests
        # CI invokes recipes individually (never `just ci`), so list this explicitly to gate PRs.
        run: just test-ansible
```

- [ ] **Step 3:** Write `deploy/ansible/tests/README.md` documenting the harness, the three
  verification signals, how to add a case, the ufw version the fixtures mirror, and the
  substring-collision known limitation.
- [ ] **Step 4:** Run `just lint-shell`, `just lint-ansible`, `just test-ansible` → all green.
- [ ] **Step 5:** Commit (single logical change: the harness).

---

### Task 7: Mutation check (acceptance gate — not committed)

- [ ] **Step 1:** Temporarily narrow the prune port pattern (drop `gdbstub_acl_tls_port`);
  run the harness; confirm `stale_present` / `multiple_stale` FAIL. Revert.
- [ ] **Step 2:** Temporarily break the exclusion (`grep -vF` a literal that never matches);
  run; confirm `steady_state` FAILs (current allow spuriously deleted). Revert.
- [ ] **Step 3:** Re-run the harness clean → all pass. Record the mutation results in the PR
  body.

---

## Self-Review

- **Spec coverage:** tag (T1), fake (T2), playbook (T3), 7 fixture cases incl. substring
  collision (T4), three-signal verification contract (T5 runner), just/CI gating + README
  (T6), mutation check (T7). All spec sections mapped.
- **Placeholder scan:** none — all shell/YAML shown verbatim.
- **Type consistency:** env var names (`FAKE_UFW_FIXTURE`/`_DELETE_LOG`/`_STATUS_MARKER`)
  match between fake (T2) and runner (T5); tag `gdbstub_acl_prune` matches role (T1),
  playbook invocation (T5).
