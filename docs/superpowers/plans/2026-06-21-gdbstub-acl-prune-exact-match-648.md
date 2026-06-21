# gdbstub_acl ufw prune exact source-field match Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the substring `grep -vF "{{ worker_cidr }}"` exclusion in the `gdbstub_acl` ufw prune with an exact equality on the ufw `From` column, so a stale `ALLOW IN` whose source string contains `worker_cidr` as a substring is pruned instead of surviving (#648).

**Architecture:** One filter in the prune pipeline in `deploy/ansible/roles/gdbstub_acl/tasks/main.yml` changes from `grep -vF` (fixed substring) to an `awk` that reads the source as the field after the `IN` direction token and compares it for exact inequality with `worker_cidr` (ADR-0201). The existing hermetic harness (`deploy/ansible/tests/`, ADR-0200) drives the real task; its `substring_collision` case flips from "no deletion" to deleting the stale lines, and two regression fixtures are added.

**Tech Stack:** Ansible (`community.general.ufw` role task, embedded bash), bash test harness driven by `ansible-playbook --tags`, a fake `ufw` shim. No Python/`src` change.

## Global Constraints

- The prune task's embedded shell stays `set -euo pipefail`-safe: the port `grep` keeps its `|| true` (grep exits 1 on no match); `awk` exits 0 on empty output and needs none.
- The matcher reads the source as **the field after the `IN` token**, not `$NF` — a trailing ufw `# comment`/`(v6)` column must not shift the matched source. Guard `$(i+1) != ""` so an unparseable row fails toward not deleting.
- String equality only — not subnet-aware (ADR-0201). The comparison assumes ufw renders `From` identically to the templated `worker_cidr`.
- Doc style: plain factual prose; avoid "critical/comprehensive/robust/elegant"; "Milestone" never "Sprint".
- Guardrails before each commit, run from repo root: `just test-ansible` (the harness), `just lint-ansible` (yamllint + ansible-lint on the role/playbook), `just lint-shell` (shellcheck/shfmt over `deploy/ansible/tests`). CI gates `test-ansible` and `lint-ansible` as their own steps.
- Worktree/branch: work on `feat/gdbstub-acl-prune-exact-match-648` (already created off `main`).

---

### Task 1: Add the two regression fixtures and flip the harness expectations (RED)

**Files:**
- Create: `deploy/ansible/tests/fixtures/prefix_collision.numbered`
- Create: `deploy/ansible/tests/fixtures/comment_column.numbered`
- Modify: `deploy/ansible/tests/run-gdbstub-acl-prune.sh` (the `substring_collision` `run_case` line + two new `run_case` lines)

**Interfaces:**
- Consumes: the existing `run_case <name> <fixture> <worker_cidr> <expected-desc>` helper and the fake `ufw` shim (unchanged).
- Produces: three asserted cases the corrected matcher must satisfy — `substring_collision` → `"7 6"`, `prefix_collision` → `"7 6"`, `comment_column` → `"7 6"`.

- [ ] **Step 1: Write `prefix_collision.numbered`** — worker `10.0.0.0/24`, stale `10.0.0.0/2` (a source that is a substring *of* the worker CIDR) on the protected ports at lines 6,7. Header comment cites ufw 0.36.x / Ubuntu 24.04 and ADR-0201.

```
# Fixture: prefix-collision (ADR-0201). Mirrors `ufw status numbered` (ufw 0.36.x, Ubuntu 24.04).
# worker_cidr=10.0.0.0/24; stale 10.0.0.0/2 (lines 6,7) is a substring *of* the worker CIDR.
# Exact source-field equality prunes it (10.0.0.0/2 != 10.0.0.0/24); pins the symmetric direction.
Status: active

     To                         Action      From
     --                         ------      ----
[ 1] 22/tcp                     ALLOW IN    Anywhere
[ 2] 16514/tcp                  ALLOW IN    10.0.0.0/24
[ 3] 47000:47099/tcp            ALLOW IN    10.0.0.0/24
[ 4] 16514/tcp                  DENY IN     Anywhere
[ 5] 47000:47099/tcp            DENY IN     Anywhere
[ 6] 16514/tcp                  ALLOW IN    10.0.0.0/2
[ 7] 47000:47099/tcp            ALLOW IN    10.0.0.0/2
```

- [ ] **Step 2: Write `comment_column.numbered`** — protected-port `ALLOW IN` rows carry a trailing ufw `# comment`; current `10.0.0.0/24` (lines 2,3) survives, stale `192.168.99.0/24` (lines 6,7) pruned. Proves the matcher reads the source column, not `$NF`.

```
# Fixture: comment-column (ADR-0201). Mirrors `ufw status numbered` (ufw 0.36.x, Ubuntu 24.04)
# with rule comments. worker_cidr=10.0.0.0/24; current allow (2,3) carries a comment and must
# survive, stale 192.168.99.0/24 (6,7) carries a comment and must be pruned. Pins that the
# matcher reads the From column (field after IN), not the last token ($NF, which the comment
# shifts off the source).
Status: active

     To                         Action      From
     --                         ------      ----
[ 1] 22/tcp                     ALLOW IN    Anywhere                   # mgmt ssh
[ 2] 16514/tcp                  ALLOW IN    10.0.0.0/24                # kdive worker
[ 3] 47000:47099/tcp            ALLOW IN    10.0.0.0/24                # kdive worker
[ 4] 16514/tcp                  DENY IN     Anywhere
[ 5] 47000:47099/tcp            DENY IN     Anywhere
[ 6] 16514/tcp                  ALLOW IN    192.168.99.0/24            # decommissioned
[ 7] 47000:47099/tcp            ALLOW IN    192.168.99.0/24            # decommissioned
```

- [ ] **Step 3: Update the runner** — in `run-gdbstub-acl-prune.sh`, change the `substring_collision` line's expected from `""` to `"7 6"`, replace the two pinned-bug comment lines above it with a note that ADR-0201 fixed it, and add `prefix_collision` + `comment_column` `run_case` lines.

- [ ] **Step 4: Run the harness against the UNCHANGED role — verify RED**

Run: `just test-ansible`
Expected: FAIL — only `substring_collision` goes red (deletes `[]`, expected `7 6`); it is the one case that reproduces the bug, because `110.0.0.0/24` *contains* `10.0.0.0/24` and `grep -vF` excludes it. `prefix_collision` and `comment_column` already pass under the buggy role (`10.0.0.0/2` does not contain `10.0.0.0/24`, and the commented current allow still contains `10.0.0.0/24`), so they are regression guards, not bug-reproductions — their job is to go red under a *wrong fix* (see Task 2 Step 5), not under the current bug.

- [ ] **Step 5: Do NOT commit yet** — RED state is not committed; proceed to Task 2 to reach GREEN in the same logical change.

---

### Task 2: Fix the prune matcher to exact source-field equality (GREEN)

**Files:**
- Modify: `deploy/ansible/roles/gdbstub_acl/tasks/main.yml` (the prune task's pipeline, the `grep -vF` line ~145)

**Interfaces:**
- Consumes: the upstream port/action `grep -E "...ALLOW IN "` output (unchanged), with its trailing-space anchor.
- Produces: line numbers of protected-port `ALLOW IN` rows whose source field != `worker_cidr`, fed to the existing `sed`/`sort -rn`/delete loop (unchanged).

- [ ] **Step 1: Replace the substring exclusion with the awk source-field matcher.** Change:

```yaml
            | { grep -vF "{{ worker_cidr }}" || true; } \
```

to:

```yaml
            | { awk -v cidr="{{ worker_cidr }}" '{ for (i = 1; i <= NF; i++) if ($i == "IN") { if ($(i + 1) != "" && $(i + 1) != cidr) print; break } }'; } \
```

- [ ] **Step 2: Update the prune task's inline comment** (the block above the task, ~line 129-136) to describe exact source-field matching instead of "source is not the current worker_cidr" via substring, citing ADR-0201 and noting the field-after-`IN` read + the non-empty guard + the canonical-`worker_cidr` assumption.

- [ ] **Step 3: Run the harness — verify GREEN**

Run: `just test-ansible`
Expected: PASS — all cases, including `substring_collision` → `[7 6]`, `prefix_collision` → `[7 6]`, `comment_column` → `[7 6]`, `steady_state`/`ufw_inactive`/`non_protected_port` → `[]`.

- [ ] **Step 4: Lint the role and the harness**

Run: `just lint-ansible` then `just lint-shell`
Expected: both PASS (no yamllint/ansible-lint/shellcheck/shfmt findings).

- [ ] **Step 5: Mutation check — two mutations, prove the new cases catch failures.** Run each, then restore the correct awk line; record both observed failures in the PR body (do not commit either mutated state):
  - **(a) Re-introduce the bug:** revert the matcher line to `grep -vF "{{ worker_cidr }}"`. Expected: `substring_collision` goes RED (deletes `[]`, expected `7 6`) — proves the bug-fix is tested. (`prefix_collision`/`comment_column` stay green under this mutation.)
  - **(b) The tempting wrong fix:** change the matcher to `awk -v cidr="{{ worker_cidr }}" '$NF != cidr'`. Expected: `comment_column` goes RED (the comment shifts `$NF` off the source, so the current `10.0.0.0/24` allows are deleted too) — proves the field-after-`IN` choice is tested.

- [ ] **Step 6: Commit Tasks 1+2 as one logical change**

```bash
git add deploy/ansible/roles/gdbstub_acl/tasks/main.yml deploy/ansible/tests/
git commit -m "fix(gdbstub_acl): prune stale allows by exact source-field match"
```

(Commit message body cites #648 + ADR-0201; ends with the `Co-Authored-By` trailer.)

---

### Task 3: Update the harness README known-limitation

**Files:**
- Modify: `deploy/ansible/tests/README.md` (the `substring_collision` table row + the "Known limitation (substring exclusion)" section + add the two new fixture rows)

**Interfaces:**
- Consumes: nothing.
- Produces: documentation consistent with the flipped behavior; no test or code dependency.

- [ ] **Step 1: Flip the `substring_collision` table row** from "survives — see Known limitation" to "stale `110.0.0.0/24` pruned by exact source-field match (ADR-0201)", and add `prefix_collision` + `comment_column` rows.

- [ ] **Step 2: Rewrite the "Known limitation (substring exclusion)" section** into a "Resolved (ADR-0201)" note: the exclusion is now exact source-field equality; record the residual canonical-`worker_cidr` assumption and that the live re-verify (off-CIDR ACL check in `deploy/ansible/README.md`) asserts the current allow survives.

- [ ] **Step 3: Run doc + harness guards**

Run: `just test-ansible` then `just docs-links` then `just docs-paths`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add deploy/ansible/tests/README.md
git commit -m "docs(gdbstub_acl): mark prune substring limitation resolved by ADR-0201"
```

---

## Self-Review

- **Spec coverage:** ADR-0201 Decision (exact source-field match) → Task 2; the `substring_collision` flip → Task 1 Step 3; the two new fixtures (`prefix_collision`, `comment_column`) → Task 1; README known-limitation update → Task 3; mutation check (verify tests catch failures) → Task 2 Step 5; live re-verify is operator runbook (out of CI, recorded in PR body / ADR). All covered.
- **Placeholder scan:** every code/fixture block is literal; no TBD/TODO.
- **Type/string consistency:** the awk line is byte-identical in the plan, the ADR snippet, and Task 2 Step 1; expected deletions `"7 6"` are consistent across fixtures and runner.
