# Local-libvirt host kernel readability and an honest check-deps report

Goal: make the local-libvirt host play declare and verify the `/boot` kernel readability that
guest-image builds require, correct the operator docs that credit a seven-line shim with doing
it, and give `just check-deps` a probe reporting that host state against the recipe that now
fixes it.

Architecture: three surfaces, no runtime code. One new Ansible task file in the
`local_worker_host` role; one report-only probe in `scripts/check-setup-deps.sh` reusing the
existing `note_manual` tier accumulator; prose and remedy-string corrections across two operator
docs, one runbook and both preflight scripts. Provisioning lands first so the probe names a path
provisioning satisfies.

Tech stack: Ansible (`ansible.builtin.find`, `ansible.builtin.file`, `ansible.builtin.getent`,
`ansible.builtin.assert`), Bash 4.3+ (`local -n` namerefs), pytest driving both scripts as
subprocesses with stubbed PATH and env overrides.

Spec: `docs/workflow/specs/2026-09-15-local-libvirt-host-kernel-readability-design.md`.
Failure model: that spec's `## Failure model` section.

Expected implementation size: 150–220 changed lines (M) — from the file map below: one ~35-line
task file, ~4 lines of role edits, ~35 lines of shell, ~35 of prose and remedy strings, ~95 of
new and edited test cases.

## Global Constraints

Transcribed from the spec and `AGENTS.md`:

- Branch `fix/local-libvirt-host-kernel-readability-2479`, base `main`.
- Guardrails: `just lint`, `just type` (whole tree, src + tests — do not narrow),
  `just lint-shell`, `just lint-ansible`, `just test-ansible`, `just records`, `just ci`.
  `ansible-core` aborts under a non-blocking stdio harness: run the Ansible recipes as
  `just lint-ansible > <file> 2>&1 < /dev/null`.
- Never pipe a gate recipe through `tail`/`head`; never append `; echo $?`. Redirect instead.
- Before `git commit`: stage, record `git diff --cached --name-only`, run `prek run`, then
  `git add -- <exactly those paths>`. Not `git add -A` / `-u`.
- Ruff line length 100. Doc style: "Milestone" never "Sprint"; avoid "critical", "robust",
  "comprehensive", "essential", "significant", "elegant".
- No `src/kdive/` change. No new ADR: the mode composes `live_vm_host`'s accepted
  `0640 root:kvm` choice with `guest_image_prereqs`'s accepted Debian-family guard.
- **Do not edit any merged ADR.** `.github/scripts/profiles/adr.sh` sets
  `APPEND_ONLY_SECTIONS="*"`, so `just records` raises `E-REWRITE` for any line dropped from a
  merged record's non-`Status` section. The `§4b` citations in ADR-0214 and ADR-0393 stay.
- ADR-0393's `future` tier stays warn-only: no tier reclassification, no exit-code change.
- The RedHat and Suse families must not be narrowed or reddened by any task here.

## File map

| Path | Owns now | Owns after |
|---|---|---|
| `deploy/ansible/roles/local_worker_host/tasks/boot_kernels.yml` | — (new) | the Debian-family `/boot` relabel and its `kvm` membership assertion |
| `deploy/ansible/roles/local_worker_host/tasks/main.yml` | role task ordering | same, plus the `boot_kernels.yml` import |
| `scripts/check-setup-deps.sh` | tiered host-dependency report | same, plus `BOOT_DIR` + `probe_boot_kernels`, widened hint heading, honest closing line, corrected runbook pointers |
| `scripts/operations/check-local-libvirt.sh` | live-host preflight | same, with the `chmod 0644` remedy replaced and three runbook pointers corrected |
| `docs/operating/providers/local-libvirt.md` | family-difference notes | same, crediting `just prepare-local-libvirt-host` |
| `docs/operating/install.md` | host-preparation walkthrough | same, naming the relabel and the upgrade re-run |
| `docs/operating/runbooks/four-method-live-run.md` | redirect stub + venv note | same, one ambiguous sentence disambiguated |
| `tests/deploy/test_live_worker_provisioning.py` | role-YAML assertions | same, plus the relabel case |
| `tests/scripts/test_check_setup_deps.py` | script behaviour | same, plus `BOOT_DIR` cases; `KDIVE_BOOT_DIR` pinned in all three launch paths |
| `tests/scripts/test_check_local_libvirt.py` | script behaviour | same, remedy and pointer assertions updated |

No caller migration and no obsolete path: every edit extends a file that already owns the
responsibility. `examples/local-libvirt/install-host.sh` stays the compatibility shim it is;
`deploy/ansible/roles/guest_image_prereqs/` is deliberately untouched (its play targets
`remote_libvirt_hosts` and runs the appliance as root, so it neither shares a host with this
play nor needs the mode, and `group: kvm` would add a prerequisite that play does not create).

## Task 1 — declare and verify the `/boot` relabel in local-libvirt provisioning

Creates `deploy/ansible/roles/local_worker_host/tasks/boot_kernels.yml`. Modifies that role's
`tasks/main.yml`. Tests `tests/deploy/test_live_worker_provisioning.py`. Serves spec Success 1;
first, because Task 2's probe names this recipe as its remedy.

**Interfaces.** Consumes `live_vm_host_worker_accounts` (defined in
`deploy/ansible/roles/local_worker_host/defaults/main.yml`, the eight `kdive-worker-N` names)
and the `kvm` group membership that `local_worker_host/tasks/worker_accounts.yml` and
`libvirt_stack/tasks/main.yml` already establish. Registers `local_worker_host_boot_kernels`,
used only inside the new file. Later work relies on the path
`deploy/ansible/roles/local_worker_host/tasks/boot_kernels.yml` and on the literals
`group: kvm` and `mode: "0640"` appearing in it.

**Verification.**

- Contract: the role declares the Debian-guarded `0640 root:kvm` relabel and its `kvm`
  membership assertion, and `main.yml` imports it. Mode: `focused-test`. Observable: the two role YAML
  files read as text from `tests/deploy/test_live_worker_provisioning.py`. Expected red:
  `FileNotFoundError` on the new task file. Green:
  `uv run python -m pytest tests/deploy/test_live_worker_provisioning.py -q -k boot_kernel`.

**Steps.**

1. Add the failing case to `tests/deploy/test_live_worker_provisioning.py`, which already
   defines `LOCAL_WORKER = ROLE.parent / "local_worker_host"`:

   ```python
   def test_local_worker_declares_boot_kernel_readability() -> None:
       """Debian/Ubuntu ship /boot/vmlinuz-* root:root 0600 and libguestfs copies a host
       kernel as the invoking user for its supermin appliance (ADR-0222, #2479), so an
       undeclared mode breaks the next clean reprovision of a local-libvirt host."""
       tasks = (LOCAL_WORKER / "tasks" / "boot_kernels.yml").read_text()
       assert "ansible.builtin.find" in tasks
       assert 'patterns: ["vmlinuz-*", "vmlinux-*"]' in tasks
       assert "group: kvm" in tasks
       # 0640 root:kvm, not 0644: live_vm_host chose group scope over undoing /boot
       # hardening for every local uid, and this role follows that choice.
       assert 'mode: "0640"' in tasks
       assert 'mode: "0644"' not in tasks
       # RedHat and Suse ship these world-readable and must not be narrowed.
       assert "ansible_facts['os_family'] == 'Debian'" in tasks
       # Declaring the mode is not proving it: the read is granted through the group, so
       # the membership is checked too -- NOT by a read as each worker (live_vm_host's
       # shape), which needs the undeclared acl package on this connection: local play.
       assert "become_user" not in tasks
       assert "getent" in tasks
       assert "live_vm_host_worker_accounts" in tasks
       assert "boot_kernels.yml" in (LOCAL_WORKER / "tasks" / "main.yml").read_text()
   ```

2. Run `uv run python -m pytest tests/deploy/test_live_worker_provisioning.py -q -k boot_kernel`
   and confirm red: `FileNotFoundError` for `boot_kernels.yml`.

3. Create `deploy/ansible/roles/local_worker_host/tasks/boot_kernels.yml`:

   ```yaml
   ---
   # Debian and Ubuntu ship /boot/vmlinuz-* root:root 0600. libguestfs builds its supermin
   # appliance by copying a host kernel as the INVOKING user, so build-fs dies with an opaque
   # "supermin exited with error status 1" (ADR-0222, #694/#1156, #2479) — probed by
   # check-local-libvirt.sh as a FAIL and by check-setup-deps.sh as a future-tier hint.
   # RedHat and Suse ship these world-readable; relabelling there would NARROW them, so the
   # block is guarded to the family that has the defect.
   - name: Make the Debian-family host kernels readable for guest-image builds
     when: ansible_facts['os_family'] == 'Debian'
     block:
       - name: Find the host kernels under /boot (vmlinuz-* x86_64, vmlinux-* ppc64le)
         ansible.builtin.find:
           paths: /boot
           patterns: ["vmlinuz-*", "vmlinux-*"]
         register: local_worker_host_boot_kernels

       - name: Make the host kernels kvm-group-readable for the worker accounts
         # 0640 root:kvm, not 0644: live_vm_host made the same choice for the same file so the
         # relaxation stays group-scoped. The fixed worker accounts (worker_accounts.yml) and
         # the operator login account (libvirt_stack) are both already in kvm. A kernel UPGRADE
         # installs a fresh 0600 file under a new name: re-run the recipe afterwards.
         ansible.builtin.file:
           path: "{{ item.path }}"
           group: kvm
           mode: "0640"
         loop: "{{ local_worker_host_boot_kernels.files }}"
         loop_control:
           label: "{{ item.path }}"

       - name: Read the kvm group the relabel grants read through
         ansible.builtin.getent:
           database: group
           key: kvm
         register: local_worker_host_kvm_group

       - name: Verify every worker account is in the kvm group
         # The mode grants read through the group, so an account outside kvm still cannot read
         # the kernel. Membership rather than a read AS each worker: this play is
         # connection: local with an unprivileged connection user, where escalating to another
         # unprivileged account falls back to setfacl (pipelining sits under [ssh_connection],
         # which the local connection does not read) and no role this play runs declares acl.
         ansible.builtin.assert:
           that:
             - item in local_worker_host_kvm_group.ansible_facts.getent_group['kvm'][2].split(',')
           fail_msg: >-
             {{ item }} is not in the kvm group, so it cannot read the relabelled host kernels
             and build-fs will still fail; re-run this role's worker_accounts.yml.
         loop: "{{ live_vm_host_worker_accounts }}"
   ```

   Tag the block `tags: [boot_kernels]` so the family guard can be driven in check mode.

4. In `deploy/ansible/roles/local_worker_host/tasks/main.yml`, insert after the
   `Prepare fixed worker accounts` import:

   ```yaml
   - name: Make the host kernels readable for guest-image builds
     ansible.builtin.import_tasks: boot_kernels.yml
   ```

5. Run `uv run python -m pytest tests/deploy/test_live_worker_provisioning.py -q`; expect every
   case in the module to pass.

6. Run `just lint-ansible > /tmp/la-2479.log 2>&1 < /dev/null`, then
   `just test-ansible > /tmp/ta-2479.log 2>&1 < /dev/null`; both exit 0.

**Acceptance criteria.** `boot_kernels.yml` exists, is Debian-guarded, finds both kernel
patterns, sets `group: kvm` / `mode: "0640"`, and verifies readability per worker account;
`main.yml` imports it; both Ansible recipes pass.

**Rollback.** Delete `boot_kernels.yml` and revert the `main.yml` edit. No host state to undo.

## Task 2 — probe `/boot` readability in `check-setup-deps.sh`

Modifies `scripts/check-setup-deps.sh`. Tests `tests/scripts/test_check_setup_deps.py`. Serves
spec Success 2. Depends on Task 1: its remedy string names the recipe Task 1 made real.

**Interfaces.** Consumes `note_manual <tier> <label> <instruction>`, which appends `<label>` to
the nameref array `<tier>_commands` and `"<label>: <instruction>"` to `manual_hints`. Defines
the readonly `BOOT_DIR` and `probe_boot_kernels()`, taking no arguments, called from
`probe_all`. Nothing later consumes either.

**Verification.**

- Contract: an unreadable kernel under `BOOT_DIR` produces the host-kernel hint naming
  `just prepare-local-libvirt-host`, without changing the exit status. Mode: `focused-test`.
  Observable: the script's stderr and returncode. Expected red: the hint string is absent.
  Green: `uv run python -m pytest tests/scripts/test_check_setup_deps.py -q -k boot`.
- Contract: a readable kernel, and an absent `BOOT_DIR`, produce no `/boot` hint. Mode:
  `focused-test`. Observable: stderr. Expected red: both fail before `KDIVE_BOOT_DIR` is
  honoured, because the script reads the real `/boot`. Green: the same command.
- Contract: the manual-hint heading covers a host-state entry. Mode: `focused-test`.
  Observable: stderr in `test_ppc64le_missing_rust_fails_with_rustup_hint`. Expected red: that
  case still demands the old heading. Green: `uv run python -m pytest
  tests/scripts/test_check_setup_deps.py -q`.

**Steps.**

1. In `tests/scripts/test_check_setup_deps.py`, pin `KDIVE_BOOT_DIR` in **all three** places
   that launch the script, not only `_run` — the other two build their own env dicts and would
   otherwise read the runner's real `/boot`:

   - in `_run`'s default `env` dict, after the `KDIVE_QEMU_LIBEXEC` entry;
   - in the interactive-TTY case's hand-built `env` (the one with `KDIVE_PYTHON`, launched
     through `subprocess.Popen` with a pty);
   - in `test_autodetects_repo_venv_under_relative_invocation`'s hand-built `env` (the one with
     `KDIVE_GUESTFS_SYS_SITE`, launched through `subprocess.run`).

   Each gets `"KDIVE_BOOT_DIR": str(tmp_path / "absent-boot"),`. In `_run`, carry the reason:

   ```python
       # Pin the host-kernel probe at an absent directory for the same reason: its default is
       # the real /boot, and a CI runner shipping 0600 kernels would otherwise add a hint to
       # every case in this module.
       "KDIVE_BOOT_DIR": str(tmp_path / "absent-boot"),
   ```

2. Add three cases to the same module:

   ```python
   def _boot(tmp_path: Path, *, readable: bool) -> Path:
       """A fake /boot holding one kernel, readable or not by the invoking user."""
       d = tmp_path / "boot"
       d.mkdir()
       kernel = d / "vmlinuz-6.8.0-124-generic"
       kernel.write_text("")
       kernel.chmod(0o644 if readable else 0o000)
       return d


   @skip_if_root
   def test_unreadable_boot_kernel_reports_the_provisioning_remedy(tmp_path: Path) -> None:
       """Debian/Ubuntu 0600 kernels break every guest-image build (ADR-0222, #2479), so
       check-deps must say so and route the operator at the recipe that declares the mode.

       Root-only: `[[ -r ]]` is true for uid 0 whatever the mode, which is why the remedy
       string says the probe reads as the invoking user.
       """
       result = _run(
           "debian",
           str(_bin(tmp_path)),
           tmp_path,
           extra_env={"KDIVE_BOOT_DIR": str(_boot(tmp_path, readable=False))},
       )

       assert "host kernel readability" in result.stderr, result.stderr
       assert "just prepare-local-libvirt-host" in result.stderr, result.stderr
       assert "reads as the invoking user" in result.stderr, result.stderr
       # A future-tier hint must not change the exit status (ADR-0393: warn only).
       assert result.returncode == 0, result.stdout


   def test_readable_boot_kernel_reports_nothing(tmp_path: Path) -> None:
       """A readable kernel is the Fedora default and the post-provisioning Debian state."""
       result = _run(
           "debian",
           str(_bin(tmp_path)),
           tmp_path,
           extra_env={"KDIVE_BOOT_DIR": str(_boot(tmp_path, readable=True))},
       )

       assert "host kernel readability" not in result.stderr, result.stderr


   def test_absent_boot_dir_reports_nothing(tmp_path: Path) -> None:
       """An unusual /boot layout must skip the probe, not fail on the literal glob."""
       result = _run(
           "debian",
           str(_bin(tmp_path)),
           tmp_path,
           extra_env={"KDIVE_BOOT_DIR": str(tmp_path / "no-such-boot")},
       )

       assert "host kernel readability" not in result.stderr, result.stderr
   ```

3. In `test_ppc64le_missing_rust_fails_with_rustup_hint`, change
   `assert "Tooling not provided by your distribution" in result.stderr` to
   `assert "Manual fixes your distribution's packages do not supply" in result.stderr`.

4. Run `uv run python -m pytest tests/scripts/test_check_setup_deps.py -q -k boot` and confirm
   all three new cases fail.

5. In `scripts/check-setup-deps.sh`, beside the existing `readonly KVM_NODE=` declaration:

   ```bash
   # libguestfs builds its supermin appliance by copying a host kernel out of this directory as
   # the INVOKING user. Overridable for tests, mirroring KDIVE_KVM_NODE and check-local-libvirt.sh.
   readonly BOOT_DIR="${KDIVE_BOOT_DIR:-/boot}"
   ```

6. Add the probe beside `probe_guestfs`:

   ```bash
   # Debian and Ubuntu ship /boot/vmlinuz-* root:root 0600, so build-fs dies with an opaque
   # "supermin exited with error status 1" before any image can be built (ADR-0222, #2479).
   # Fedora ships them world-readable. Report-only: a privileged /boot mutation is outside this
   # script's remediation contract (ADR-0393), and the recipe named below declares it instead.
   # `-r` is true for uid 0 whatever the mode, so the remedy says which user the probe read as.
   probe_boot_kernels() {
     local k
     for k in "${BOOT_DIR}"/vmlinuz-* "${BOOT_DIR}"/vmlinux-*; do
       [[ -e "${k}" ]] || continue # no-match glob stays literal under no-nullglob; skip it
       [[ -r "${k}" ]] && continue
       note_manual future "host kernel readability" \
         "a kernel under ${BOOT_DIR} is not readable (this probe reads as the invoking user), so libguestfs cannot build its appliance and no guest image can be built — run 'KDIVE_LIFECYCLE_WITNESS_DATABASE_URL=... just prepare-local-libvirt-host', which declares the mode, or for a one-off: sudo chgrp kvm ${BOOT_DIR}/vmlinu?-* && sudo chmod 0640 ${BOOT_DIR}/vmlinu?-*"
       return
     done
   }
   ```

7. Call it from `probe_all`, immediately after `probe_guestfs "${distro}"`:

   ```bash
     probe_boot_kernels
   ```

8. Replace the manual-hint heading `printf "\nTooling not provided by your distribution:\n" >&2`
   with `printf "\nManual fixes your distribution's packages do not supply:\n" >&2`.

9. Replace the closing line
   `printf "\nRequired dependencies are present; optional items above are not yet needed.\n"`
   with
   `printf "\nRequired dependencies are present. The items above are not needed for the core dev loop; the live_vm and guest-image tiers do need them.\n"`.

10. Run `uv run python -m pytest tests/scripts/test_check_setup_deps.py -q`; expect the module
    green. Then `just lint-shell`; expect exit 0.

**Acceptance criteria.** The script honours `KDIVE_BOOT_DIR`, reports the host-kernel entry only
for an unreadable kernel, keeps exit 0 for a future-tier-only report, and prints the widened
heading and corrected closing line; `just lint-shell` passes.

## Task 3 — reconcile the remedies, the docs, and the dangling runbook pointers

Modifies `scripts/check-setup-deps.sh`, `scripts/operations/check-local-libvirt.sh`,
`docs/operating/providers/local-libvirt.md`, `docs/operating/install.md`,
`docs/operating/runbooks/four-method-live-run.md`. Tests
`tests/scripts/test_check_setup_deps.py`, `tests/scripts/test_check_local_libvirt.py`. Serves
spec Success 3 and 4; last, because it names what Tasks 1 and 2 built. Touches **no ADR**.

**Interfaces.** Consumes the heading `## Wire the worker venv (drgn + libguestfs)` in
`docs/operating/runbooks/four-method-live-run.md`. Exposes nothing.

**Verification.**

- Contract: both scripts' guestfs hints name a heading that exists in the runbook. Mode:
  `focused-test`. Observable: each script's stderr in its existing venv-failure case
  (`test_guestfs_hint_names_the_venv_symlink_remedy`,
  `test_missing_venv_bindings_fails_with_hint`). Expected red: the new assertions demand the
  section name while the scripts still print `section 4b`. Green: `uv run python -m pytest
  tests/scripts/test_check_setup_deps.py tests/scripts/test_check_local_libvirt.py -q -k
  "venv_symlink_remedy or missing_venv_bindings"`.
- Contract: `check-local-libvirt.sh`'s host-kernel remedy no longer prescribes `chmod 0644`.
  Mode: `focused-test`. Observable: stderr in `test_unreadable_host_kernel_fails_with_chmod_hint`.
  Expected red: that case still asserts `chmod 0644 {boot}/vmlinu?-*`. Green: `uv run python -m
  pytest tests/scripts/test_check_local_libvirt.py -q -k unreadable_host_kernel`.
- Contract: the prose corrections in `local-libvirt.md`, `install.md` and
  `four-method-live-run.md`. Mode: `task-test-not-applicable`. The changed surface is
  operator-facing prose with no executable or structural consumer; `just docs-links` validates
  the link targets, and asserting on the sentences themselves would be a prose snapshot.

**Steps.**

1. In `tests/scripts/test_check_setup_deps.py::test_guestfs_hint_names_the_venv_symlink_remedy`,
   after the existing `assert "four-method-live-run.md" in result.stderr`:

   ```python
       # The old "section 4b" pointer named a heading that no longer exists in that runbook.
       assert "Wire the worker venv" in result.stderr, result.stderr
       assert "section 4b" not in result.stderr, result.stderr
   ```

2. In `tests/scripts/test_check_local_libvirt.py::test_missing_venv_bindings_fails_with_hint`,
   after `assert "python3-libguestfs" in result.stderr`, add the same two assertions.

3. In `tests/scripts/test_check_local_libvirt.py::test_unreadable_host_kernel_fails_with_chmod_hint`,
   replace `assert f"chmod 0644 {boot}/vmlinu?-*" in result.stderr` with:

   ```python
       # 0640 root:kvm, matching what `just prepare-local-libvirt-host` declares — a 0644
       # remedy here would hand the operator a wider mode than provisioning establishes.
       assert "just prepare-local-libvirt-host" in result.stderr
       assert f"chgrp kvm {boot}/vmlinu?-*" in result.stderr
       assert f"chmod 0640 {boot}/vmlinu?-*" in result.stderr
       assert "0644" not in result.stderr
   ```

4. Run `uv run python -m pytest tests/scripts/test_check_setup_deps.py
   tests/scripts/test_check_local_libvirt.py -q -k "venv_symlink_remedy or
   missing_venv_bindings or unreadable_host_kernel"` and confirm all three fail.

5. In `scripts/check-setup-deps.sh`, replace the three runbook pointers: the `probe_guestfs`
   hint's trailing clause becomes `— see docs/operating/runbooks/four-method-live-run.md, "Wire
   the worker venv (drgn + libguestfs)"`; the `guestfs_sys_site` comment's `The exact logic in
   runbook §4b.` becomes `The exact logic is in that runbook's "Wire the worker venv" section.`;
   and the future-tier comment citing `four-method-live-run.md §4b` names the same section.

6. In `scripts/operations/check-local-libvirt.sh`, repoint the **two venv-binding** occurrences
   (lines 261 and 265) of `— see docs/operating/runbooks/four-method-live-run.md section 4b` to
   `— see docs/operating/runbooks/four-method-live-run.md, "Wire the worker venv (drgn + libguestfs)"`.
   The third occurrence is on the `INSTALL_STAGING` remedy (line 281), which that section does
   not cover: **drop** the ` — see docs/operating/runbooks/four-method-live-run.md section 4b`
   clause there rather than repointing it. The `sudo install -d -o "$USER" …` remedy is
   self-contained without it.

7. In the same script, replace the host-kernel remedy string with:

   ```bash
       "run this preflight as the worker user; if Debian/Ubuntu (root:0600 kernels): run 'KDIVE_LIFECYCLE_WITNESS_DATABASE_URL=... just prepare-local-libvirt-host', which declares the mode, or for a one-off: sudo chgrp kvm ${BOOT_DIR}/vmlinu?-* && sudo chmod 0640 ${BOOT_DIR}/vmlinu?-* (the glob matches both arches; re-apply after a kernel upgrade)"
   ```

   The recipe needs that variable: `justfile:55` exits 2 without it, so a remedy naming the
   bare recipe would fail when followed.

8. Re-run the command from step 4; expect green.

9. In `docs/operating/runbooks/four-method-live-run.md`, replace the closing sentence
   `Reprovision the owning environment rather than copying incompatible native bindings between
   Python versions.` with:

   ```markdown
   Reprovision the owning environment rather than copying a native binding between *differing*
   Python versions; that is the case `scripts/check-setup-deps.sh` also refuses, ABI-checking
   both interpreters before it links anything.
   ```

10. In `docs/operating/providers/local-libvirt.md`, rewrite the **Host kernel permissions**
    bullet so the remedy is the recipe:

    ```markdown
    - **Host kernel permissions.** Debian/Ubuntu ship `/boot/vmlinuz-*` as `root:root 0600`,
      which the libguestfs appliance cannot read as a non-root user, so
      `just prepare-local-libvirt-host` relabels them `root:kvm 0640` and then verifies that
      each worker account can read them
      ([ADR-0222](../../adr/0222-ubuntu-build-fs-libguestfs-diagnostics.md)). Fedora ships them
      world-readable and is left alone. A Debian/Ubuntu kernel upgrade installs a fresh `0600`
      file under a new name: re-run the recipe afterwards. `just check-deps` and
      `just check-local-libvirt` both report the unfixed state.
    ```

11. In `docs/operating/install.md`, extend `The recipe installs and configures the local
    virtualization stack, worker lifecycle, project venv, and guestfs binding.` to also name
    the Debian/Ubuntu `/boot` kernel modes that guest-image builds need, and add that a kernel
    upgrade installs a fresh `0600` kernel so the recipe should be re-run after one.

12. Run `just docs-links` and `just lint-shell`; each exits 0.

**Acceptance criteria.** `rg -n 'section 4b|§4b' scripts/` returns no match; both scripts' hints
name the runbook section that exists; neither preflight script prescribes `chmod 0644` for a
host kernel; `local-libvirt.md` and `install.md` credit `just prepare-local-libvirt-host`; no
file under `docs/adr/` is modified.

## Self-review against the spec

Spec Success 1 → Task 1. Success 2 → Task 2. Success 3 → Task 3 steps 5–6 and its acceptance
criterion. Success 4 → Task 3 steps 7 and 10–11. Success 5 → the guardrail runs in each task
plus the `just ci` and `just records` runs before pushing; `just records` is protected by the
Global Constraint forbidding any merged-ADR edit. Every spec Validation row maps to a
Verification entry above, and the single `task-test-not-applicable` names its concrete reason.
Every borrowed name above was read back from the worktree before this plan was frozen: the
shell helpers and `KVM_NODE` in `scripts/check-setup-deps.sh`; `LOCAL_WORKER`/`ROLE` in
`tests/deploy/test_live_worker_provisioning.py`; `_bin`, `_run`, `skip_if_root` and the four
named test cases in the two script test modules; `live_vm_host_worker_accounts` in
`deploy/ansible/roles/local_worker_host/defaults/main.yml`; and the per-account read check in
`deploy/ansible/roles/live_vm_host/tasks/verify.yml`, which Task 1 step 3 deliberately does
not reuse (branch review pass 1: it needs the undeclared `acl` package on a local connection).

Deferrals carried into implementation: none. Follow-up candidates recorded in the spec's
out-of-scope list: the `§4b` citations in merged ADR-0214 and ADR-0393, and the four other
`install-host.sh` attributions in `local-libvirt.md` plus the matching ADR-0640 reference.
