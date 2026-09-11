# Static `svirt_image_t` label for kdive images — implementation plan (#2424)

**Goal.** Make provisioning succeed on an SELinux-enforcing RedHat-family host with sVirt
confinement intact, by labeling kdive's image directories `svirt_image_t` instead of
`virt_image_t`, and by correcting the operator text that still prescribes the old label or reports
the bug as open.

**Architecture.** One surface only: host preparation under `examples/local-libvirt/`. A single
sourced shell helper applies the label to one directory and migrates an existing rule on the same
pattern; three call sites use it. No Python behavior changes, and the rendered domain XML is
untouched — a first design cycle proposed a per-disk `<seclabel>` and the review retired it
(ADR-0639, rejected alternatives).

**Tech stack.** Bash (installer scripts), pytest (driving the helper through a stub PATH).

**Spec:** `docs/workflow/specs/2026-09-11-svirt-image-label-2424-design.md`
**Decision:** `docs/adr/0639-static-svirt-image-label-for-session-mode-domains.md`

Expected implementation size: 150–200 changed lines (M) — derived from the file map below: one new
~30-line shell helper, one new ~85-line test module, three small script edits, and nine prose or
help-text corrections. Task 3 changes no repository file.

## Global Constraints

- Python 3.14, managed with `uv`. Ruff line length **100**, lint set `E,F,I,UP,B,SIM`. `ty` runs
  with strict defaults over **src and tests**.
- Shell is **Bash**; `just lint-shell` runs
  `shfmt -f scripts deploy/compose deploy/remote-libvirt-guest-helpers deploy/ansible/tests examples | xargs shellcheck`,
  so a new file under `examples/` is linted by both automatically. Both example scripts run under
  `set -euo pipefail`.
- A `# shellcheck source=` directive in this repository is always paired with `disable=SC1091` —
  see `examples/local-libvirt/build-image.sh:17`. Match that, or `just lint-shell` fails.
- Guardrails: `just lint`, `just type`, `just test`; full gate
  `just ci > <file> 2>&1 < /dev/null`. Never pipe a gate through `tail`/`head`; never append
  `; echo $?`.
- Before `git commit`: `just format` for Python-only changes; for shell/Markdown, stage, run
  `prek run`, then `git add -- <exactly the paths you staged>`.
- Doc-style: use **Milestone**, never "Sprint"; avoid "critical", "robust", "comprehensive",
  "elegant" in ADRs, specs, commit messages and comments.
- ADRs live under `docs/adr/`, named `NNNN-kebab-title.md`, monotonic numbers never reused. This
  change owns **0639** and touches no other ADR file. There is no ADR index (ADR-0504).
- `semanage fcontext -l` prints each rule's pattern **verbatim** — `/var/lib/kdive/rootfs(/.*)?` —
  space-padded into columns. The backslashes in the existing greps at `install-host.sh:277` and
  `build-image.sh:52` are BRE escaping of `.` and `*`, not characters in the output. This plan
  removes both greps, so no code depends on the format either way.
- `svirt_image_t` is listed in `/etc/selinux/targeted/contexts/customizable_types` and
  `virt_image_t` is not. A plain `restorecon` therefore relabels *into* `svirt_image_t` but
  silently skips relabeling *out* of it; only a rollback needs `-F`.
- Verified host baselines for the live proof: Fedora Linux 44 Server, `libvirt-daemon-12.0.0-3.fc44`,
  `qemu-kvm-core-10.2.2-1.fc44`, `selinux-policy-44.3-1.fc44`; Rocky Linux 10.2,
  `libvirt-daemon-11.10.0-12.4.el10_2`, `selinux-policy-42.1.18-4.el10`.

## File map

| File | Action | Answerable for |
|---|---|---|
| `examples/local-libvirt/selinux-label.sh` | create | the one labeling+migration function, sourceable for tests |
| `examples/local-libvirt/install-host.sh` | modify | sources the helper; labels rootfs and install staging; two comment corrections |
| `examples/local-libvirt/build-image.sh` | modify | sources the helper; `label_for_qemu` calls it; header comment correction |
| `tests/scripts/test_selinux_label.py` | create | drives the helper with stubbed `getenforce`/`semanage`/`restorecon` |
| `tests/providers/local_libvirt/test_install.py` | modify | one assertion string, tracking the `install.py` remediation edit |
| `examples/local-libvirt/README.md` | modify | one table cell |
| `docs/operating/providers/local-libvirt.md` | modify | the SELinux bullet; delete the resolved `## Known limitation` section |
| `src/kdive/config/core_settings.py` | modify | `KDIVE_INSTALL_STAGING` help text |
| `docs/guide/reference/config.md` | regenerate | the generated row for that setting |
| `src/kdive/providers/local_libvirt/lifecycle/install.py` | modify | the staging-root remediation string |
| `deploy/ansible/roles/live_vm_host/tasks/main.yml` | modify | one comment clause |
| `deploy/ansible/inventory/group_vars/live_vm_runners.yml` | modify | one comment clause |
| the ADR-0639 record | modify | Status Proposed → Accepted |

---

## Task 1 — the labeling helper and its three call sites

**Where it fits.** This is the whole functional fix.

**Creates:** `examples/local-libvirt/selinux-label.sh`, `tests/scripts/test_selinux_label.py`
**Modifies:** `examples/local-libvirt/install-host.sh`, `examples/local-libvirt/build-image.sh`

**Interfaces.** This task defines, and nothing earlier provides:

```bash
# examples/local-libvirt/selinux-label.sh
kdive_label_svirt_image <directory>   # returns 0 on success or no-op; non-zero if the policy
                                       # store can't be modified (both -m and -a fail)
```

Task 2 consumes nothing from this task.

### Verification

| Contract | Mode | Detail |
|---|---|---|
| A pattern with no rule gets one `svirt_image_t` rule added, then `restorecon` | `focused-test` | `tests/scripts/test_selinux_label.py::test_adds_rule_when_absent`; red before the helper exists — `bash -c 'source <missing>; kdive_label_svirt_image …'` exits 127, so `subprocess.run(..., check=True)` raises `CalledProcessError`; green via `uv run python -m pytest tests/scripts/test_selinux_label.py -q` |
| A pattern carrying a stale rule is **modified**, never added twice | `focused-test` | `…::test_migrates_stale_rule` — asserts a `-m -t svirt_image_t` call and no second rule; red against the shipped `-a`-only logic, whose presence check short-circuits and records no write at all |
| The helper no-ops when SELinux is not enforcing | `focused-test` | `…::test_noop_when_not_enforcing` — asserts no `semanage`/`restorecon` invocation was recorded |
| A missing `semanage` reports and returns 0 | `focused-test` | `…::test_reports_missing_semanage` — asserts nothing was written and the exit status is 0 |
| The three installer call sites | `task-test-not-applicable` | `tests/scripts/test_install_host_gates.py` stops the installer at its `sudo` preflight, far above these lines, and driving the rest needs a real enforcing host with root — the live proof in Task 3. The branching this task adds lives in the helper, which the four tests above cover. |
| The two comment-literal corrections (step 5) | `task-test-not-applicable` | shell comments with no executable consumer. The acceptance criterion below is checked with `rg -n virt_image_t examples/local-libvirt/`, which is a grep over prose, not a contract a test can fail meaningfully. |

### Steps

1. Create `examples/local-libvirt/selinux-label.sh`:

```bash
#!/usr/bin/env bash
# Label a kdive image directory so a confined QEMU domain can use it (ADR-0639, #2424).
#
# svirt_t may read virt_image_t but may not write or map it, and the unprivileged session
# libvirt daemon never performs the dynamic relabel that closes that gap on a privileged
# daemon. So the static label has to be one the confined domain can use: svirt_image_t:s0,
# which every domain reaches by MCS dominance whatever categories libvirt draws for it. A
# privileged daemon relabels from svirt_image_t exactly as it did from virt_image_t, so this
# label is correct under both.
#
# Sourced by install-host.sh and build-image.sh; sourcing it runs nothing.

kdive_label_svirt_image() {
  local directory="$1" pattern="${1}(/.*)?"

  command -v getenforce >/dev/null 2>&1 || return 0
  [[ "$(getenforce)" == "Enforcing" ]] || return 0

  if ! command -v semanage >/dev/null 2>&1; then
    echo "SELinux is enforcing but semanage is missing; install policycoreutils-python-utils" >&2
    echo "and label ${directory} svirt_image_t before provisioning." >&2
    return 0
  fi

  # -m modifies an existing rule, -a adds a missing one, and each fails when the other case
  # applies. Trying -m first reaches the same state on a fresh host and on one installed before
  # ADR-0639 (whose rule is still virt_image_t), without parsing `semanage fcontext -l` output.
  sudo semanage fcontext -m -t svirt_image_t "${pattern}" 2>/dev/null ||
    sudo semanage fcontext -a -t svirt_image_t "${pattern}"
  sudo restorecon -R "${directory}"
}
```

2. In `examples/local-libvirt/install-host.sh`, source the helper immediately after `example_dir`
   is resolved (`install-host.sh:16`; the variable is `example_dir`, not `script_dir`):

```bash
# shellcheck source=examples/local-libvirt/selinux-label.sh disable=SC1091
source "${example_dir}/selinux-label.sh"
```

3. Replace the whole of step 7b (`install-host.sh:268-285`, the
   `if command -v getenforce …` block ending at its `fi`) with:

```bash
# 7b. SELinux labels for every directory a confined domain opens. Provisioning writes each
#     System's overlay under rootfs/ and direct-kernel boot maps the baseline kernel/initrd from
#     there; the install plane points a live domain's <os> at kernel/initrd under install/.
#     svirt_t can do neither against virt_image_t (ADR-0639). build-image.sh owns the nested
#     rootfs/local rule and migrates it itself; the base images under it are read-only backing
#     files, which svirt_t may read under either label.
step "SELinux svirt_image_t on the kdive image directories"
kdive_label_svirt_image /var/lib/kdive/rootfs
kdive_label_svirt_image /var/lib/kdive/install
```

   Do **not** add a creation step for `/var/lib/kdive/install`. Step 6 at `install-host.sh:254-259`
   runs `deploy/systemd/install-live-worker-lifecycle.sh`, which already creates it at `:611-613`
   with `install -d -o "$operator" -g "$libvirt_group" -m 2770`, so the directory exists before
   step 7b and a second creator would apply a different owner/group posture in the same run.

4. In `examples/local-libvirt/build-image.sh`, source the helper after `example_dir` is resolved
   (`build-image.sh:15`, beside the existing `env.sh` source at `:17`):

```bash
# shellcheck source=examples/local-libvirt/selinux-label.sh disable=SC1091
source "${example_dir}/selinux-label.sh"
```

   and reduce `label_for_qemu` (`build-image.sh:44-55`) to:

```bash
# Label the rootfs directory on SELinux-enforcing hosts only (Fedora/EL). A qcow2 published from
# a $HOME workspace can carry data_home_t, which the confined domain cannot read (ADR-0639).
label_for_qemu() {
  kdive_label_svirt_image "${rootfs_dir}"
}
```

5. Correct the two remaining `virt_image_t` comment literals in these scripts, which describe the
   rootfs directory this change relabels:

   - `install-host.sh:106` — "build-image.sh needs `semanage` to label the rootfs directory
     virt_image_t on an SELinux-enforcing host" → "… to label the rootfs directory svirt_image_t
     on an SELinux-enforcing host".
   - `build-image.sh:9` — "on an SELinux-enforcing host the rootfs directory is labeled
     virt_image_t so the qemu user can read it" → "… is labeled svirt_image_t so the confined
     domain can use it".

6. Create `tests/scripts/test_selinux_label.py`, following the `_stub`/`_bindir` pattern already in
   `tests/scripts/test_install_host_gates.py` and carrying
   `pytestmark = requires_bash(4, 3, …)` from `tests.host_capabilities` (the helper uses `[[ ]]`
   and `local`). The harness sources the helper rather than executing it —
   `bash -c 'source <helper>; kdive_label_svirt_image /var/lib/kdive/rootfs'` with `PATH` set to a
   `tmp_path` stub directory — so nothing is ever labeled. Three points are not obvious and carry
   the design:

   - The `sudo` stub is **transparent** (`exec "$@"`), so the `semanage` and `restorecon` stubs
     observe exactly the argv the helper passed.
   - Each stub appends its argv to one log file, and that log is the whole assertion surface.
   - The `semanage` stub's exit status for the `-m` arm is the test parameter: `1` stands for a
     fresh host where only `-a` can succeed, `0` for a host that already carries a rule. This is
     what separates the two rule-writing tests.

   The four tests assert, against that log:

   | Test | Asserts |
   |---|---|
   | `test_adds_rule_when_absent` | `-m` was tried, `-a -t svirt_image_t <pattern>` followed, then `restorecon -R <dir>` |
   | `test_migrates_stale_rule` | `-m -t svirt_image_t <pattern>` ran, **no** `-a` followed, then `restorecon -R <dir>` |
   | `test_noop_when_not_enforcing` | the log is empty |
   | `test_reports_missing_semanage` | the log is empty and the exit status is 0 |

7. Run the focused tests red first, before steps 1–6:
   `uv run python -m pytest tests/scripts/test_selinux_label.py -q`
   Expect: collection succeeds, all four fail with `subprocess.CalledProcessError` and exit
   status 127 — the function is undefined after `source` of a non-existent file.
8. After steps 1–6, run the same command. Expect: `4 passed`.
9. `just lint-shell` — expect no output and exit 0.
10. Stage, `prek run`, re-add exactly the staged paths, commit:
    `fix(local-libvirt): label kdive image directories svirt_image_t`

### Acceptance criteria

- `kdive_label_svirt_image` exists in one file and is called twice by `install-host.sh` and once
  by `build-image.sh`; neither script contains a `semanage fcontext` invocation of its own.
- No `virt_image_t` **directive** remains in `install-host.sh` or `build-image.sh`: no `semanage`
  argument, no remedy string, and no usage-header claim about the label a path carries. Explanatory
  prose that names the old label to contrast it with the new one is expected and stays — the
  helper's header and the step 7b comment, both specified verbatim by steps 1 and 3, do exactly
  that. A plain `rg virt_image_t` cannot express this: `svirt_image_t` contains `virt_image_t`, so
  every correct occurrence matches too. Use `rg -n --pcre2 '(?<!s)virt_image_t'` to see the bare
  ones, then read them.
- The four focused tests pass; `just lint-shell` is green.

---

## Task 2 — correct the operator-facing text, and ratify the ADR

**Where it fits.** After Task 1 the project's own documentation tells operators the feature is
broken and prescribes the label that breaks it. This closes that, and flips ADR-0639 to Accepted
in the PR that implements it, as `docs/adr/README.md` requires.

**Modifies:** `examples/local-libvirt/README.md`,
`docs/operating/providers/local-libvirt.md`, `src/kdive/config/core_settings.py`,
`docs/guide/reference/config.md`, `src/kdive/providers/local_libvirt/lifecycle/install.py`,
`tests/providers/local_libvirt/test_install.py`,
`deploy/ansible/roles/live_vm_host/tasks/main.yml`,
`deploy/ansible/inventory/group_vars/live_vm_runners.yml`, the ADR-0639 record

**Interfaces.** Consumes nothing and provides nothing; no signature changes.

### Verification

| Contract | Mode | Detail |
|---|---|---|
| The generated config table matches the changed `Setting` help text | `focused-test` | `just config-docs-check` — red immediately after editing `core_settings.py` and before regenerating, green after; CI gates this recipe individually |
| ADR-0639 carries a valid, non-Proposed status while `src/` cites it | `focused-test` | `just adr-status-check` — red while the record says `Proposed` and `install.py` cites ADR-0639, green once flipped to `Accepted` |
| The staging-root remediation string names the new label | `focused-test` | `tests/providers/local_libvirt/test_install.py:1443`. Change the assertion to `assert "svirt_image_t" in remedy` **first** and run it red against the unmodified `install.py`, then edit `install.py:571`. Green via `uv run python -m pytest tests/providers/local_libvirt/test_install.py -q -k staging`. Order matters: `"virt_image_t"` is a substring of `"svirt_image_t"`, so the original assertion stays green after the source edit and cannot witness this contract. |
| Prose corrections | `task-test-not-applicable` | documentation wording with no executable consumer; `just docs-links` and `just docs-paths` cover link and path integrity |

### Steps

1. `examples/local-libvirt/README.md` — in the `build-image.sh` table row, change "label the
   rootfs directory `virt_image_t` on SELinux hosts" to "label the rootfs directory
   `svirt_image_t` on SELinux hosts (ADR-0639)".
2. `docs/operating/providers/local-libvirt.md:76-78` — replace the **SELinux** bullet body with:
   "Fedora and Enterprise Linux run SELinux enforcing, so `install-host.sh` and `build-image.sh`
   label the kdive image directories `svirt_image_t` for the confined domain (ADR-0639).
   `install-host.sh` installs `policycoreutils-python-utils` for the `semanage` that needs."
3. Same file — delete the whole `## Known limitation — SELinux and per-System overlays` section:
   lines **108-122**, from the `##` heading through the blank line before `## Preflight` at line
   123. It describes a resolved defect and hands the reader `setenforce 0`, which ADR-0639
   rejects. Add no replacement section; the SELinux bullet above now carries the operative
   statement.
4. `src/kdive/config/core_settings.py` — in `INSTALL_STAGING.help`, change "on SELinux hosts with
   the virt_image_t label" to "on SELinux hosts with the svirt_image_t label (ADR-0639)".
5. Regenerate the config table and confirm: `just config-docs` (the generator recipe, `justfile:588`),
   then `just config-docs-check` (`justfile:592`) — expect exit 0. Stage the regenerated
   `docs/guide/reference/config.md` with the source edit; CI gates the check recipe individually.
6. `src/kdive/providers/local_libvirt/lifecycle/install.py:569-571` — change the remedy string's
   tail from "on SELinux hosts give it the virt_image_t label" to "on SELinux hosts give it the
   svirt_image_t label (ADR-0639)". Update the one assertion that reads it,
   `tests/providers/local_libvirt/test_install.py:1443`, from `assert "virt_image_t" in remedy`
   to `assert "svirt_image_t" in remedy`.

   **Do the assertion first, then the source.** `"virt_image_t"` is a substring of
   `"svirt_image_t"`, so the original assertion passes against either string and would never go
   red. With the assertion tightened and `install.py` untouched,
   `uv run python -m pytest tests/providers/local_libvirt/test_install.py -q -k staging` is red;
   after the `install.py` edit it is green.
7. `deploy/ansible/roles/live_vm_host/tasks/main.yml:1929` — change the closing clause from
   "sVirt label (the SELinux virt_image_t equivalent RHEL required)." to "sVirt label. The
   SELinux equivalent RHEL requires is a static svirt_image_t label on the image directories
   (ADR-0639)." Touch no other line in that file.
8. `deploy/ansible/inventory/group_vars/live_vm_runners.yml:3` — the clause "Both labeled
   virt_image_t, both traversable." asserts labeling the role does not perform: `main.yml:1926-1929`
   records that the Ubuntu target uses AppArmor and needs no static sVirt label, and
   `rg -n 'sefcontext|setype' deploy/ansible/` finds no such task anywhere in the tree. Replace the
   clause with "Both traversable; the Ubuntu runner confines qemu with AppArmor and needs no static
   sVirt label." Comment only — no variable, task, or value changes, within exclusion 2.
9. the ADR-0639 record — change `## Status` from `Proposed` to `Accepted (2026-09-11)` and delete
   the ratification note beneath it.
10. `just lint && just type && just adr-status-check && just docs-links && just docs-paths &&
    just config-docs-check` — expect every one green.
11. `just lint-ansible < /dev/null` — expect exit 0. (ansible-core aborts on non-blocking stdin;
    redirect it.)
12. Stage, `prek run`, re-add exactly the staged paths, commit:
    `docs(local-libvirt): record the svirt_image_t label and retire the SELinux limitation`

### Acceptance criteria

- No operator-facing text prescribes `virt_image_t` for a path Task 1 relabels, and none reports
  #2424 as an open limitation.
- The `qemu:///system` references in `docs/operating/runbooks/` and `src/kdive/testing/live_vm.py`
  are untouched, as the spec's Scope records.
- ADR-0639 is `Accepted`; `just adr-status-check` and `just config-docs-check` are green.

---

## Task 3 — the live proof and the full gate

**Where it fits.** Completion criteria 6 and 7 are the only ones no earlier task discharges. The
helper tests cover the labeling logic; nothing below the `sudo` preflight of `install-host.sh` runs
anywhere but on a real enforcing host.

**Modifies:** nothing in the repository. This task produces evidence for the PR body.

### Verification

| Contract | Mode | Detail |
|---|---|---|
| A System provisions to `ready` under SELinux enforcing | live proof | both RedHat-family targets, procedure below |
| The domain runs confined and produces no denial | live proof | `svirt_t` with MCS categories; no `denied` AVC for a path under the kdive image directories |
| The install plane's staged `kernel`/`initrd` carry the new label (criterion 2, second half) | live proof | step 5; provisioning alone never reaches this path |
| A base image left at `virt_image_t` under `rootfs/local` still serves as a backing file | live proof | step 6; the one claim ADR-0639 rests on an inference rather than a measurement |
| The repository guardrail suite is green | `focused-test` | `just ci`, bare — **already run on the reviewed HEAD: exit 0, 18470 passed, 30 skipped** |

### Steps

1. On each target — the Fedora 44 host and the Rocky 10.2 host — confirm the starting state:
   `getenforce` reports `Enforcing`, and destroy **and undefine** any domain left over from a
   permissive-mode session. Such a domain is not a counterexample and must not be reused; an
   undefine also avoids a System/domain id collision on re-provision.
2. **Record the pre-state before touching anything:** `sudo semanage fcontext -l -C | grep kdive`.
   This is what turns step 3 from "the rule is right" into "the rule was migrated" — the `-m` arm
   is the only genuinely new behaviour in the helper, and on a fresh host step 3 exercises `-a`
   instead and the migration path never runs against real `semanage`.
3. Run the installer from the branch checkout: `examples/local-libvirt/install-host.sh`. Re-run the
   same listing and diff it against step 2, expecting `svirt_image_t` on each pattern. Record which
   arm (`-m` migration or `-a` addition) each host actually took.
4. Bring the stack up (`scripts/live-stack/up.sh`) and provision a System through the ordinary
   worker path. Assert it reaches `ready` with no `setenforce 0` and no `security_driver` change.

   **Do not run `build-image.sh`.** Stage a prebuilt image instead, by the path
   `docs/operating/providers/local-libvirt.md:99-103` documents. The build-time customization boot
   opens `config.require(LIBVIRT_URI)` (`lifecycle/rootfs/customization_boot.py:163`), which under
   this stack is the **session** daemon, against a workspace defaulting to
   `$XDG_DATA_HOME/kdive/build/images`. Measured on both targets 2026-09-11: `svirt_t` gets neither
   `write` nor `map` on that path's policy default `data_home_t`, and `svirt_home_t` — which the
   directory happens to carry on the Fedora target — grants `write` but **not `map`**, so a
   direct-kernel customization boot cannot map its `kernel`/`initrd` there under either. That is a
   second defect of the same family as #2424, on the build path, and is out of scope here; it must
   not be allowed to block criterion 6. Record explicitly in the PR that `build-image.sh` was not
   exercised — "it succeeded" and "it was not run" are different evidence.
5. While the domain runs, record the provisioning assertions:
   - `ps -eZ | grep qemu-system` shows the process as `svirt_t:s0:c<i>,c<j>`.
   - `ls -Z` on the System's overlay and its baseline `kernel`/`initrd` shows `svirt_image_t`.
   - `journalctl -k --since <start>` carries no `denied` record for a path under
     `/var/lib/kdive/rootfs` or `/var/lib/kdive/install`. Read the **journal**, not `ausearch`,
     which does not surface these denials on either host (ADR-0639 Context).
6. **Exercise the install plane** — provisioning does not reach it, so without this criterion 2's
   install-staging half goes unproven while the run still reports green. Perform an install through
   the ordinary worker path, then assert:
   - `ls -Z /var/lib/kdive/install/<system-id>/<run-id>/kernel` and `…/initrd` show
     `svirt_image_t`.
   - the journal carries no `denied` record naming a path under `/var/lib/kdive/install`.
7. **Measure the `rootfs/local` read-only claim.** ADR-0639 states that leaving the nested rule to
   `build-image.sh` is safe because base images there are read-only backing files and `svirt_t` may
   read `virt_image_t`. Every other load-bearing claim in that record carries a measurement; this
   one carries an inference. With a base image under `/var/lib/kdive/rootfs/local` left at
   `virt_image_t` (its state on an upgraded host after `install-host.sh` alone), confirm a System
   backed by it provisions and runs with no `denied` record naming that path. Record the result in
   ADR-0639's Consequences the way the other measurements are recorded — and if it is denied, the
   nested call belongs in `install-host.sh` after all.
8. Record in the PR body which arms ran on which target, and the observed process and file labels.

### Acceptance criteria

- Both targets reached `ready` under enforcing, with the step 5 assertions recorded for each.
- The install-plane arm (step 6) and the `rootfs/local` measurement (step 7) are recorded.
- `just ci` exited 0.
- The PR states that `build-image.sh` was not exercised, and why.
- If either target cannot run an arm, the PR body says so explicitly and names the blocker rather
  than reporting the criterion as met.

---

## Rollback and cleanup

`git revert`-clean in the repository. On a host already migrated, reverting the repository does
**not** restore the old label. An operator who needs that runs, for each of the three patterns the
two scripts own — `/var/lib/kdive/rootfs(/.*)?` and `/var/lib/kdive/install(/.*)?` from
`install-host.sh`, `/var/lib/kdive/rootfs/local(/.*)?` from `build-image.sh`:

```sh
sudo semanage fcontext -m -t virt_image_t '/var/lib/kdive/rootfs(/.*)?'
sudo restorecon -R -F /var/lib/kdive/rootfs
```

`-F` is required in this direction and not the other: `svirt_image_t` is a customizable type, so a
plain `restorecon` will not relabel out of it. This returns the host to the failing state #2424
describes.

## Deferrals carried into this plan

Every design-review finding from both passes, and every scope-audit finding, was accepted and
applied. Two deferrals are carried, matching the spec's "Covered elsewhere":

- **The build-time customization boot hits the same denial class on the build workspace.**
  Measured on both targets 2026-09-11: `svirt_t` gets neither `write` nor `map` on `data_home_t`,
  and `svirt_home_t` grants `write` but not `map`, so the direct-kernel customization boot cannot
  map `kernel`/`initrd` from `$XDG_DATA_HOME/kdive/build/images` under the session daemon. Owner:
  reported as a follow-up in the PR, its own issue — operator decision 2026-09-11, on the grounds
  that it is a distinct path from the provisioning surface this charter covers. Its only effect on
  this change is that Task 3 stages a prebuilt image instead of building on the target.

- **`cannot limit core file size … Operation not permitted` on `kdive-build-*` domains.** Observed
  on the Fedora 44 target during design, and independent of labeling: it is an `RLIMIT_CORE`
  failure, and nothing here touches `RLIMIT_CORE`. A related condition is already owned by
  `docs/operating/providers/local-libvirt.md:87-91`, which records that classic sudo zeroes
  `RLIMIT_CORE` so the lifecycle installer raises it before launching the session daemon. Whether
  the observed failure is that condition or a distinct one is **not** established. Owner: reported
  as a follow-up candidate in the PR body, not fixed here. It is a risk to Task 3 — if it recurs on
  a target, the live-proof arm cannot complete there, and Task 3's acceptance criteria require
  saying so rather than reporting the criterion met.
