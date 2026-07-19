# Self-hosted Rocky 10 KVM runner (codified) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Codify a reproducible, arch-parameterized Rocky Linux 10 self-hosted GitHub Actions KVM runner — built to sub-issue A's `live_vm` environment contract — as Ansible roles under `deploy/ansible`, plus a runbook.

**Architecture:** Two new roles (`live_vm_host` for the host-contract delta, `github_runner` for runner registration) reuse the existing `libvirt_stack` + `libvirt_pool_net` roles via a new `playbooks/runner.yml` against a new `live_vm_runners` inventory group. The whole contract targets one service account (`github_runner_user`); a two-part in-play gate (`check-local-libvirt.sh` + the role's own SELinux/traversal/XDG/venv assertions) is the codified readiness check. A `just test-ansible` regression harness drives `github_runner`'s pure-logic branches in isolation.

**Tech Stack:** Ansible (`ansible-core==2.21.1`), collections `community.general==13.1.0` / `ansible.posix==2.2.0` (already in `requirements.yml`), Bash (harness), RHEL-family (`dnf`, `semanage`/`restorecon`, `loginctl`, systemd), `uv` (venv), GitHub `actions/runner`.

**Spec:** `docs/superpowers/specs/2026-07-19-selfhosted-kvm-runner-1291-design.md`
**ADR:** `docs/adr/0387-selfhosted-kvm-runner-host-codification.md`

## Global Constraints

- **Branch:** `feat/selfhosted-kvm-runner-1291`; base `main`. Never commit on `main`.
- **Guardrails (run before every commit; CI gates these individually):** `just lint-ansible` (yamllint + ansible-lint over `deploy/ansible`), `just test-ansible`, `just lint-shell` (shellcheck), `just docs-links`, `just docs-paths`, `just adr-status-check`, and `prek run` (secret-scan, EOF, trailing-ws). `just lint-workflows` only if a workflow file is touched (this plan touches none — CI job is sub-issue D).
- **FQCN required:** ansible-lint mandates fully-qualified module names (`ansible.builtin.*`, `community.general.*`, `ansible.posix.*`). Every task needs `changed_when`/`creates`/`register` where a `command`/`shell` runs. Name every task and play (task names start with a capital letter — `name[casing]`).
- **`var-naming[no-role-prefix]` covers `set_fact`/`register`, not just `defaults/`:** every variable a role *defines* in-task — every `register:` and `set_fact:` key — must start with the **exact** containing-role name plus underscore (`live_vm_host_…` in `live_vm_host`, `github_runner_…` in `github_runner`). A truncated prefix like `live_vm_` is rejected. Cross-role shared vars go in `group_vars` (exempt). This is a whole-tree lint error, so an unprefixed in-task var fails `just lint-ansible`.
- **Doc-style guard (project-wide):** plain, factual prose in all docs, comments, and commit messages; never "critical", "crucial", "essential", "significant", "comprehensive", "robust", "elegant", "seamless", "Sprint". Use "Milestone".
- **Secrets:** `github_runner_registration_token` is `no_log`, supplied at runtime via `--extra-vars`/vault — never written to `host_vars`, defaults, or a commit. The prek `detect-secrets` hook gates this.
- **Commit style:** Conventional commits, imperative ≤72-char subject, one logical change per commit, ending with the trailer:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- **Single service account:** `github_runner_user` (default `github-runner`) is the subject of every host-contract step — groups, staging ownership, linger, XDG, venv, and the gate's `become_user`. It is deliberately distinct from `ansible_user_id` (the connection user).
- **Arch-additive:** no x86 hard-coding. Emulator, runner asset, label token, and packages resolve from `ansible_architecture` via maps. The one non-additive step (the `actions/runner` binary has no ppc64le asset) is the `github_runner_tarball_url` override seam.
- **B scope only:** host + runner + runbook. Do **not** author the `live-vm` CI job (matrix, schedule trigger, env preflight) — that is sub-issue D.

---

## File Structure

**Create:**
- `deploy/ansible/roles/live_vm_host/defaults/main.yml` — vars for the contract delta.
- `deploy/ansible/roles/live_vm_host/meta/main.yml` — galaxy metadata (RHEL/Debian platforms).
- `deploy/ansible/roles/live_vm_host/tasks/main.yml` — groups, toolchain, /boot, venv, staging, SELinux, linger.
- `deploy/ansible/roles/live_vm_host/tasks/verify.yml` — the two-part gate (included at end of `main.yml`).
- `deploy/ansible/roles/github_runner/defaults/main.yml` — version, sha256, arch map, user, url, service toggle.
- `deploy/ansible/roles/github_runner/meta/main.yml` — galaxy metadata.
- `deploy/ansible/roles/github_runner/tasks/main.yml` — arch resolve, download, idempotence, register, service, liveness.
- `deploy/ansible/playbooks/runner.yml` — the runner bring-up playbook.
- `deploy/ansible/inventory/host_vars/rock10-runner.yml` — the x86_64 runner host.
- `deploy/ansible/inventory/group_vars/live_vm_runners.yml` — cross-role shared contract vars (kept out of role `defaults/` to satisfy the `no-role-prefix` lint rule).
- `deploy/ansible/tests/github_runner_preflight.yml` — the harness driver playbook.
- `deploy/ansible/tests/run-github-runner-preflight.sh` — the regression harness runner.
- `deploy/ansible/tests/fake-config-sh` — a fake `config.sh` (records/refuses calls).
- `self-hosted-kvm-runner.md` under `docs/operating/runbooks/` — the runbook.

**Modify:**
- `deploy/ansible/inventory/hosts.yml` — add the `live_vm_runners` group.
- `justfile` — extend the `test-ansible` recipe to run the new harness.
- `deploy/ansible/README.md` — add a runner section / pointer.
- `AGENTS.md` — one-line pointer to the runner build (in the `live_vm` conventions area).

---

## Task 1: Scaffold roles, inventory group, and the runner playbook

**Where it fits:** Establishes the lint-clean skeleton every later task fills in, so each subsequent task's `lint-ansible` gate is meaningful.

**Files:**
- Create: `deploy/ansible/roles/live_vm_host/{meta,defaults,tasks}/main.yml`
- Create: `deploy/ansible/roles/github_runner/{meta,defaults,tasks}/main.yml`
- Create: `deploy/ansible/playbooks/runner.yml`
- Create: `deploy/ansible/inventory/host_vars/rock10-runner.yml`
- Modify: `deploy/ansible/inventory/hosts.yml`

**Interfaces:**
- Produces: the `live_vm_runners` group; `playbooks/runner.yml` applying `libvirt_stack` → `libvirt_pool_net` → `live_vm_host` → `github_runner`; the role var names later tasks fill (`github_runner_user`, `live_vm_staging_dir`, `install_staging_dir`, `live_vm_venv`, `github_runner_*`).

- [ ] **Step 1: Create the `live_vm_host` role skeleton.**

`deploy/ansible/roles/live_vm_host/meta/main.yml`:
```yaml
---
galaxy_info:
  role_name: live_vm_host
  author: kdive
  description: live_vm environment-contract delta for a self-hosted KVM CI runner (#1291).
  license: MIT
  min_ansible_version: "2.21"
  platforms:
    - name: EL
      versions: ["10"]
dependencies: []
```

`deploy/ansible/roles/live_vm_host/defaults/main.yml` — only role-prefixed,
role-owned vars live here; ansible-lint's `production` profile enforces
`var-naming[no-role-prefix]`, so a non-`live_vm_host_` name in a role default is a
lint error. The cross-role shared contract vars go in group_vars (Step 4 below),
which the rule exempts.
```yaml
---
# Kernel-debug toolchain the live_vm contract needs beyond libvirt_stack.
live_vm_host_packages:
  - drgn
  - crash
  - makedumpfile
  - kexec-tools
  - kdump-utils
  - gdb
  - python3-libguestfs
  - policycoreutils-python-utils  # semanage
  - git
```

`deploy/ansible/roles/live_vm_host/tasks/main.yml`:
```yaml
---
- name: Placeholder for live_vm_host (filled by later tasks)
  ansible.builtin.debug:
    msg: "live_vm_host role scaffold"
```

- [ ] **Step 2: Create the `github_runner` role skeleton.**

`deploy/ansible/roles/github_runner/meta/main.yml`: mirror the `live_vm_host` meta with `role_name: github_runner` and description "Register a self-hosted GitHub Actions runner for the KVM live-VM tier (#1291)."

`deploy/ansible/roles/github_runner/defaults/main.yml`:
```yaml
---
github_runner_user: github-runner
github_runner_install_dir: /opt/actions-runner
# Pinned together per bump (see runbook). Look up the current stable release + its
# published linux-x64 SHA-256 at implementation time; do not assume from memory.
github_runner_version: "0.0.0-SET-AT-IMPLEMENTATION"
github_runner_sha256: "SET-AT-IMPLEMENTATION"
# ansible_architecture -> {asset: <actions/runner asset arch or ''>, label: <arch label token>}.
# actions/runner ships linux-x64 / linux-arm64 / linux-arm only; ppc64le has NO upstream asset.
github_runner_arch_map:
  x86_64: {asset: x64, label: x64}
  ppc64le: {asset: "", label: ppc64le}
# Override the derived asset URL (the ppc64le seam: point at a self-built runner tarball).
github_runner_tarball_url: ""
github_runner_repo_url: ""            # https://github.com/<org>/<repo> — required
github_runner_registration_token: "" # runtime --extra-vars / vault only; NEVER committed
github_runner_extra_labels: [self-hosted, kvm]
# Secrets pointer for the provisioned-System family (B sets the pointer; C/D populate material).
github_runner_secrets_root: /var/lib/kdive/secrets
# Install stopped until the trusted-events posture is applied (closes the B-before-D RCE window).
github_runner_service_enabled: false
```

`deploy/ansible/roles/github_runner/tasks/main.yml`: a single `ansible.builtin.debug` placeholder task like Step 1.

- [ ] **Step 3: Add the inventory group and host_vars.**

Modify `deploy/ansible/inventory/hosts.yml` — add a sibling group under `children`:
```yaml
    live_vm_runners:
      hosts:
        rock10-runner:
```

`deploy/ansible/inventory/host_vars/rock10-runner.yml`:
```yaml
---
# x86_64 Rocky 10 self-hosted KVM runner (epic #1289 sub-issue B). ppc64le is a drop-in:
# a new host under live_vm_runners with github_runner_tarball_url pointing at a ppc64le runner.
ansible_host: rock10-runner.dev.pdx.drc.nz
github_runner_repo_url: https://github.com/randomparity/kdive
# github_runner_registration_token supplied at runtime, never here.
```

`deploy/ansible/inventory/group_vars/live_vm_runners.yml` — the cross-role shared
contract vars. They live here (not a role `defaults/`) because ansible-lint's
`production` profile rejects a role default whose name is not `<role>_`-prefixed,
and these are consumed by BOTH `live_vm_host` and `github_runner`. group_vars are
exempt from that rule and apply to every host in the play. (`github_runner_user`
stays in `github_runner/defaults` — it is already role-prefixed and the localhost
test harness, which does not load this group_vars, needs a default for it.)
```yaml
---
# Throwaway-rootfs overlay area (KDIVE_LIVE_VM_ROOTFS's parent) + the provisioned-System
# install staging check-local-libvirt.sh asserts. Both labeled virt_image_t, both traversable.
live_vm_staging_dir: /var/lib/kdive/live-vm
install_staging_dir: /var/lib/kdive/install
# Persistent repo checkout + venv the worker's guestfs/drgn import uses; D reuses via KDIVE_PYTHON.
live_vm_venv: /opt/kdive
live_vm_repo_url: https://github.com/randomparity/kdive.git
live_vm_repo_version: main
```

> **Live-validation correction (#1291):** an earlier draft installed a foreign
> qemu emulator here for cross-arch TCG. Removed — this native-KVM host runs the
> `live_vm` native tier only (cross-arch TCG rides hosted runners, ADR-0353), and
> RHEL/Rocky's `qemu-kvm` ships no `qemu-system-*` foreign targets to install.

- [ ] **Step 4: Create the runner playbook.**

`deploy/ansible/playbooks/runner.yml`:
```yaml
---
- name: Bring up a self-hosted KVM live-VM runner (arch-additive)
  hosts: live_vm_runners
  become: true
  gather_facts: true
  roles:
    - libvirt_stack
    - libvirt_pool_net
    - live_vm_host
    - github_runner
```

- [ ] **Step 5: Verify lint + syntax.**

Run:
```bash
just lint-ansible
ANSIBLE_CONFIG=deploy/ansible/ansible.cfg uv run --with 'ansible-core==2.21.1' \
  ansible-playbook -i deploy/ansible/inventory/hosts.yml \
  deploy/ansible/playbooks/runner.yml --syntax-check
```
Expected: yamllint + ansible-lint report no errors; `--syntax-check` prints the playbook name with no error.

- [ ] **Step 6: Commit.**
```bash
git add deploy/ansible/roles/live_vm_host deploy/ansible/roles/github_runner \
        deploy/ansible/playbooks/runner.yml deploy/ansible/inventory/hosts.yml \
        deploy/ansible/inventory/host_vars/rock10-runner.yml \
        deploy/ansible/inventory/group_vars/live_vm_runners.yml
git commit -m "feat(1291): scaffold live_vm_host + github_runner roles and runner.yml"
```

---

## Task 2: `live_vm_host` — service-account groups, toolchain, /boot readability, linger

**Where it fits:** The non-venv, non-staging half of the host-contract delta. Every step targets `github_runner_user`.

**Files:**
- Modify: `deploy/ansible/roles/live_vm_host/tasks/main.yml`

**Interfaces:**
- Consumes: `github_runner_user`, `live_vm_host_packages` (Task 1 defaults).
- Produces: the service account in `kvm`/`libvirt`, the toolchain installed, `/boot` kernels readable, `enable-linger` set — all asserted by Task 5's gate.

- [ ] **Step 1: Replace the placeholder with the service-account + toolchain tasks.**

`deploy/ansible/roles/live_vm_host/tasks/main.yml`:
```yaml
---
- name: Ensure the runner service account exists
  ansible.builtin.user:
    name: "{{ github_runner_user }}"
    system: true
    create_home: true
    shell: /bin/bash

- name: Add the runner service account to kvm and libvirt
  ansible.builtin.user:
    name: "{{ github_runner_user }}"
    groups: [kvm, libvirt]
    append: true

- name: Install the kernel-debug toolchain (RHEL-family)
  ansible.builtin.dnf:
    name: "{{ live_vm_host_packages }}"
    state: present
  when: ansible_os_family == 'RedHat'

- name: Find the host kernels under /boot (vmlinuz-* x86, vmlinux-* ppc64le)
  # Stock RHEL/Rocky ships /boot/vmlinuz-* 0600 root:root; libguestfs' supermin appliance
  # build (ADR-0222, #694/#1156) — probed by check-local-libvirt.sh as a FAIL — needs them
  # readable by the non-root runner user. Re-apply after a kernel upgrade (see runbook).
  ansible.builtin.find:
    paths: /boot
    patterns: ["vmlinuz-*", "vmlinux-*"]
  register: live_vm_host_boot_kernels

- name: Make the host kernels group-readable for the service account
  # file+mode reports changed only when a mode actually changes, so a converged re-run is
  # 0-changed (the spec's idempotence bar) — unlike a blanket chmod with changed_when: true.
  ansible.builtin.file:
    path: "{{ item.path }}"
    mode: "0644"
  loop: "{{ live_vm_host_boot_kernels.files }}"
  loop_control:
    label: "{{ item.path }}"

- name: Enable linger so /run/user/<uid> exists with no login session
  ansible.builtin.command: "loginctl enable-linger {{ github_runner_user }}"
  args:
    creates: "/var/lib/systemd/linger/{{ github_runner_user }}"
```

- [ ] **Step 2: Verify lint + syntax.**
```bash
just lint-ansible
```
Expected: no ansible-lint errors. (Note: the `chmod` shell task uses a glob, which `command` cannot expand — `shell` with an explicit `changed_when`/`failed_when` is correct and lint-clean.)

- [ ] **Step 3: Commit.**
```bash
git add deploy/ansible/roles/live_vm_host/tasks/main.yml
git commit -m "feat(1291): live_vm_host groups, toolchain, /boot readability, linger"
```

---

## Task 3: `live_vm_host` — provision the ABI-matched venv for the guestfs/drgn import

**Where it fits:** Makes `check-local-libvirt.sh`'s `import guestfs, drgn` probe pass reproducibly on a fresh host, at a persistent path D reuses.

**Files:**
- Modify: `deploy/ansible/roles/live_vm_host/tasks/main.yml` (append)

**Interfaces:**
- Consumes: `live_vm_venv`, `live_vm_repo_url`, `live_vm_repo_version`, `github_runner_user`.
- Produces: a venv at `{{ live_vm_venv }}/.venv` with drgn + the libguestfs symlinks; `KDIVE_PYTHON` = `{{ live_vm_venv }}/.venv/bin/python` (the contract Task 5's gate and sub-issue D consume).

- [ ] **Step 1: Append the venv-provisioning tasks.**
```yaml
- name: Install uv system-wide (the venv builder; not in the debug toolchain)
  # The service account runs `uv sync` below; /opt is root-owned so uv must be on the
  # system PATH, not a per-user install. pip installs the console script to /usr/local/bin.
  ansible.builtin.pip:
    name: uv
    state: present

- name: Create the venv root owned by the service account
  # /opt is root-owned 0755, so a non-root `git` clone cannot mkdir it — create it first.
  ansible.builtin.file:
    path: "{{ live_vm_venv }}"
    state: directory
    owner: "{{ github_runner_user }}"
    group: "{{ github_runner_user }}"
    mode: "0755"

- name: Check out the project source for the venv
  ansible.builtin.git:
    repo: "{{ live_vm_repo_url }}"
    dest: "{{ live_vm_venv }}"
    version: "{{ live_vm_repo_version }}"
  become_user: "{{ github_runner_user }}"

- name: Build the venv against the SYSTEM interpreter (ABI-match python3-libguestfs)
  # uv defaults to a managed CPython whose minor version may differ from the distro's
  # python3-libguestfs .so; pin to /usr/bin/python3 so the symlinked native module ABI-matches.
  ansible.builtin.command:
    cmd: uv sync --python /usr/bin/python3 --group live
    chdir: "{{ live_vm_venv }}"
    creates: "{{ live_vm_venv }}/.venv/bin/python"
  become_user: "{{ github_runner_user }}"

- name: Locate the system libguestfs binding files
  ansible.builtin.find:
    paths: /usr/lib64/python3*/site-packages
    patterns: ["guestfs.py", "libguestfsmod*.so"]
    recurse: true
  register: live_vm_host_libguestfs_files

- name: Symlink the libguestfs binding into the venv site-packages (no PyPI wheel exists)
  ansible.builtin.file:
    src: "{{ item.path }}"
    dest: >-
      {{ live_vm_venv }}/.venv/lib/python{{ ansible_python.version.major }}.{{
      ansible_python.version.minor }}/site-packages/{{ item.path | basename }}
    state: link
    force: true
  loop: "{{ live_vm_host_libguestfs_files.files }}"
  become_user: "{{ github_runner_user }}"
```

- [ ] **Step 2: Verify lint.**
```bash
just lint-ansible
```
Expected: no errors. If ansible-lint flags the `git`/`command` tasks for missing `changed_when`, note the `creates:` guard on the `uv sync` command satisfies idempotence; the `git` module reports its own change state.

- [ ] **Step 3: Commit.**
```bash
git add deploy/ansible/roles/live_vm_host/tasks/main.yml
git commit -m "feat(1291): live_vm_host provisions the ABI-matched guestfs/drgn venv"
```

---

## Task 4: `live_vm_host` — staging dirs + `virt_image_t` on both

**Where it fits:** Creates and labels the two staging areas so system-mode boots are not sVirt-denied; both dirs are asserted by Task 5.

**Files:**
- Modify: `deploy/ansible/roles/live_vm_host/tasks/main.yml` (append)

**Interfaces:**
- Consumes: `live_vm_staging_dir`, `install_staging_dir`, `github_runner_user`.
- Produces: both dirs at mode `0755`, owned by the service account, with a persistent `virt_image_t` fcontext + world-traversable parents.

- [ ] **Step 1: Append the staging + SELinux tasks.**
```yaml
- name: Create the staging directories owned by the service account
  ansible.builtin.file:
    path: "{{ item }}"
    state: directory
    owner: "{{ github_runner_user }}"
    group: "{{ github_runner_user }}"
    mode: "0755"
  loop:
    - "{{ live_vm_staging_dir }}"
    - "{{ install_staging_dir }}"

- name: Ensure the /var/lib/kdive parent is world-traversable
  ansible.builtin.file:
    path: /var/lib/kdive
    state: directory
    mode: "0755"

- name: Persist the virt_image_t fcontext on both staging trees
  community.general.sefcontext:
    target: "{{ item }}(/.*)?"
    setype: virt_image_t
    state: present
  loop:
    - "{{ live_vm_staging_dir }}"
    - "{{ install_staging_dir }}"
  when: ansible_selinux.status is defined and ansible_selinux.status == 'enabled'
  notify: Relabel staging dirs

- name: Apply the SELinux label now (do not wait for the handler)
  ansible.builtin.command: "restorecon -R {{ item }}"
  loop:
    - "{{ live_vm_staging_dir }}"
    - "{{ install_staging_dir }}"
  register: live_vm_host_restorecon
  changed_when: live_vm_host_restorecon.stdout | length > 0
  when: ansible_selinux.status is defined and ansible_selinux.status == 'enabled'
```

> Do **not** add the `import_tasks: verify.yml` line here — `verify.yml` does not exist yet, and
> `import_tasks` is a static include resolved at parse time, so ansible-lint's internal
> `--syntax-check` would fatally error on a missing file and `just lint-ansible` would go red. The
> import line is added in Task 5 Step 1, in the same commit that creates `verify.yml`.

- [ ] **Step 2: Add the handler** (`deploy/ansible/roles/live_vm_host/handlers/main.yml`):
```yaml
---
- name: Relabel staging dirs
  ansible.builtin.command: "restorecon -R {{ item }}"
  loop:
    - "{{ live_vm_staging_dir }}"
    - "{{ install_staging_dir }}"
  changed_when: false
```

- [ ] **Step 3: Verify lint.**
```bash
just lint-ansible
```
Expected: no errors.

- [ ] **Step 4: Commit.**
```bash
git add deploy/ansible/roles/live_vm_host/tasks/main.yml deploy/ansible/roles/live_vm_host/handlers/main.yml
git commit -m "feat(1291): live_vm_host stages and virt_image_t-labels both dirs"
```

---

## Task 5: `live_vm_host` — the two-part host-contract gate

**Where it fits:** The codified readiness check the spec names — `check-local-libvirt.sh` for what it covers, plus the role's own assertions for the delta it does not.

**Files:**
- Create: `deploy/ansible/roles/live_vm_host/tasks/verify.yml`
- Modify: `deploy/ansible/roles/live_vm_host/tasks/main.yml` (append the import line — in the same commit that creates `verify.yml`, so no lint pass ever sees a dangling static import)

**Interfaces:**
- Consumes: all Task 2–4 outputs + `live_vm_venv`, staging dirs, `github_runner_user`.
- Produces: a play failure if any contract item is unmet.

- [ ] **Step 1: Write the gate, then wire it into `main.yml`.**

`deploy/ansible/roles/live_vm_host/tasks/verify.yml`:
```yaml
---
# Resolve the service-account uid up front; ansible_facts.getent_passwd is populated only
# after this task runs (gather_facts does NOT populate it), and the /run/user path needs it.
- name: Look up the service account's passwd entry
  ansible.builtin.getent:
    database: passwd
    key: "{{ github_runner_user }}"

- name: Record the service-account uid
  ansible.builtin.set_fact:
    live_vm_host_uid: "{{ ansible_facts.getent_passwd[github_runner_user][1] }}"

# Part 1: check-local-libvirt.sh (KVM / daemons / venv-import / network / install-staging
# writability / /boot readability), run AS the service account after a connection reset so the
# just-added kvm/libvirt group membership is live, with KDIVE_PYTHON pointing at the venv.
- name: Reset the connection so new group membership is read
  ansible.builtin.meta: reset_connection

- name: Run check-local-libvirt.sh as the service account
  ansible.builtin.command: ./scripts/check-local-libvirt.sh
  args:
    chdir: "{{ live_vm_venv }}"
  environment:
    KDIVE_PYTHON: "{{ live_vm_venv }}/.venv/bin/python"
    KDIVE_INSTALL_STAGING: "{{ install_staging_dir }}"
  become_user: "{{ github_runner_user }}"
  changed_when: false

# Part 2: the delta check-local-libvirt.sh does not cover.
# Read the ACTUAL on-disk label with `ls -Zd` (its 4th field is the SELinux context, which
# includes the type). NOT `matchpathcon -V` — on a correctly-labeled path that prints
# "<path> verified." with no type string, so a `virt_image_t in stdout` assert would be inverted.
- name: Read the SELinux label of both staging dirs
  ansible.builtin.command: "ls -Zd {{ item }}"
  loop:
    - "{{ live_vm_staging_dir }}"
    - "{{ install_staging_dir }}"
  register: live_vm_host_labels
  changed_when: false
  when: ansible_selinux.status is defined and ansible_selinux.status == 'enabled'

- name: Assert both staging dirs carry virt_image_t
  ansible.builtin.assert:
    that: "'virt_image_t' in item.stdout"
    fail_msg: "{{ item.item }} is not labeled virt_image_t (system-mode boot would be sVirt-denied)"
  loop: "{{ live_vm_host_labels.results }}"
  loop_control:
    label: "{{ item.item }}"
  when: ansible_selinux.status is defined and ansible_selinux.status == 'enabled'

- name: Assert the service account is in kvm and libvirt
  ansible.builtin.command: "id -nG {{ github_runner_user }}"
  register: live_vm_host_groups
  changed_when: false
  failed_when: "'kvm' not in live_vm_host_groups.stdout.split() or 'libvirt' not in live_vm_host_groups.stdout.split()"

- name: Assert /run/user/<uid> exists for the service account
  ansible.builtin.stat:
    path: "/run/user/{{ live_vm_host_uid }}"
  register: live_vm_host_xdg
  failed_when: not live_vm_host_xdg.stat.exists
```

> The service-unit `XDG_RUNTIME_DIR=` assertion is deferred to `github_runner` (Task 7), which owns the unit/`.env` file.

Now append the import to the end of `deploy/ansible/roles/live_vm_host/tasks/main.yml` (this
is the first commit in which `verify.yml` exists, so the static import always resolves):
```yaml
- name: Run the host-contract gate
  ansible.builtin.import_tasks: verify.yml
```

- [ ] **Step 2: Verify lint + syntax.**
```bash
just lint-ansible
ANSIBLE_CONFIG=deploy/ansible/ansible.cfg uv run --with 'ansible-core==2.21.1' \
  ansible-playbook -i deploy/ansible/inventory/hosts.yml \
  deploy/ansible/playbooks/runner.yml --syntax-check
```
Expected: no errors.

- [ ] **Step 3: Exercise the Part-2 gate logic in-branch (dev KVM host).** `--syntax-check` and
ansible-lint do not evaluate the `ls -Zd` command output or the `getent` uid build, so run the two
load-bearing mechanisms against real state once, non-invasively (no host mutation), to prove the
corrected assertions evaluate the way the gate expects:
```bash
# The label read the gate relies on: an existing virt_image_t path must contain the type string
# (this is what would have caught the matchpathcon -V inversion).
ls -Zd /var/lib/libvirt/images | grep -q virt_image_t && echo "ls -Zd label read OK"
# The getent uid mechanism the /run/user path build relies on (any existing user):
ANSIBLE_CONFIG=deploy/ansible/ansible.cfg uv run --with 'ansible-core==2.21.1' \
  ansible localhost -m ansible.builtin.getent -a 'database=passwd key=root' | grep -q '"root"' \
  && echo "getent passwd mechanism OK"
```
Expected: both lines print `OK`. (A full `ansible-playbook runner.yml` apply — which creates the
service account, chmods `/boot`, clones the repo, and registers a runner — is the operator step in
Task 9's note, not this branch.)

- [ ] **Step 4: Commit.**
```bash
git add deploy/ansible/roles/live_vm_host/tasks/verify.yml deploy/ansible/roles/live_vm_host/tasks/main.yml
git commit -m "feat(1291): live_vm_host two-part host-contract gate"
```

---

## Task 6: `github_runner` regression harness (write the test first — TDD)

**Where it fits:** The behavioral test for Task 7's pure-logic branches. Written first so Task 7 is implemented against a failing harness, mirroring the `gdbstub_acl` harness (`deploy/ansible/tests/`).

**Files:**
- Create: `deploy/ansible/tests/github_runner_preflight.yml`
- Create: `deploy/ansible/tests/run-github-runner-preflight.sh`
- Create: `deploy/ansible/tests/fake-config-sh`
- Modify: `justfile` (the `test-ansible` recipe)

**Interfaces:**
- Consumes: the `github_runner` role's tags — `github_runner_register` (the download+register branch) — and vars `github_runner_registration_token`, `github_runner_arch_map`, `github_runner_tarball_url`, `github_runner_install_dir`. Task 7 must expose these.
- Produces: three asserted behaviors: token fail-closed, arch fail-loud, already-registered skip.

- [ ] **Step 1: Write the driver playbook.**

`deploy/ansible/tests/github_runner_preflight.yml`:
```yaml
---
- name: Exercise github_runner registration preflight in isolation (regression harness)
  hosts: localhost
  connection: local
  gather_facts: true
  roles:
    - role: github_runner
```

- [ ] **Step 2: Write the fake `config.sh`.**

`deploy/ansible/tests/fake-config-sh` (records that it ran; the harness asserts it did/didn't):
```bash
#!/usr/bin/env bash
# Fake actions-runner config.sh: record invocation so the harness can assert whether the
# register branch reached it. Never contacts GitHub.
echo "config.sh $*" >>"${FAKE_CONFIG_LOG:?FAKE_CONFIG_LOG unset}"
exit 0
```

- [ ] **Step 3: Write the harness runner**, modeled on `run-gdbstub-acl-prune.sh` (same env exports, `mktemp` workdir, `ANSIBLE_ROLES_PATH`). It runs three cases against `github_runner_preflight.yml` with a fake `config.sh` on `PATH` and `github_runner_install_dir` pointed into the workdir:

`deploy/ansible/tests/run-github-runner-preflight.sh`:
```bash
#!/usr/bin/env bash
# Regression harness for the github_runner registration preflight (#1291). Drives the REAL
# role in isolation via ansible-playbook against localhost, with a fake config.sh, asserting
# the two security-sensitive fail paths and the idempotence skip — no GitHub token, no runner.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/../../.." && pwd)"
playbook="$here/github_runner_preflight.yml"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
install -m 0755 "$here/fake-config-sh" "$work/config.sh"

export PATH="$work:$PATH"
export ANSIBLE_ROLES_PATH="$repo_root/deploy/ansible/roles"
export ANSIBLE_PYTHON_INTERPRETER="${ANSIBLE_PYTHON_INTERPRETER:-$(command -v python3)}"
export ANSIBLE_NOCOWS=1
export ANSIBLE_LOCALHOST_WARNING=False
export ANSIBLE_INVENTORY_UNPARSED_WARNING=False
export FAKE_CONFIG_LOG="$work/config.log"

fail=0
# --tags github_runner_register isolates the registration branch (arch resolve + marker stat +
# fail-closed/fail-loud + download + config.sh), exactly as the gdbstub harness isolates its
# prune task. The svc.sh-install / .env / systemd tasks are UNtagged, so they never run here —
# they need root/systemd and cannot execute in a localhost harness.
play() { ansible-playbook "$playbook" --tags github_runner_register -e "@$1" >"$2" 2>&1; }

# Case 1: token fail-closed — empty token in the register branch must fail, no config.sh run.
: >"$FAKE_CONFIG_LOG"
cat >"$work/case1.yml" <<'YAML'
github_runner_repo_url: https://github.com/x/y
github_runner_registration_token: ""
github_runner_install_dir: "PLACEHOLDER"
YAML
sed -i "s#PLACEHOLDER#$work/runner1#" "$work/case1.yml"
if play "$work/case1.yml" "$work/case1.out"; then
  echo "FAIL case1: empty token did not fail the play"; fail=1
elif [[ -s "$FAKE_CONFIG_LOG" ]]; then
  echo "FAIL case1: config.sh ran despite empty token"; fail=1
else echo "ok case1: token fail-closed"; fi

# Case 2: arch fail-loud — an arch with no asset and no override URL must fail loud.
: >"$FAKE_CONFIG_LOG"
cat >"$work/case2.yml" <<'YAML'
github_runner_repo_url: https://github.com/x/y
github_runner_registration_token: tok
github_runner_tarball_url: ""
github_runner_arch_map:
  x86_64: {asset: "", label: x64}
github_runner_install_dir: "PLACEHOLDER"
YAML
sed -i "s#PLACEHOLDER#$work/runner2#" "$work/case2.yml"
if play "$work/case2.yml" "$work/case2.out"; then
  echo "FAIL case2: missing asset+override did not fail"; fail=1
elif ! grep -qi 'ppc64le\|no upstream asset\|github_runner_tarball_url' "$work/case2.out"; then
  echo "FAIL case2: failure message did not name the arch seam"; fail=1
else echo "ok case2: arch fail-loud"; fi

# Case 3: already-registered skip — .runner marker present => no token needed, config.sh not run.
: >"$FAKE_CONFIG_LOG"
mkdir -p "$work/runner3"; echo '{}' >"$work/runner3/.runner"; echo '{}' >"$work/runner3/.credentials"
cat >"$work/case3.yml" <<'YAML'
github_runner_repo_url: https://github.com/x/y
github_runner_registration_token: ""
github_runner_install_dir: "PLACEHOLDER"
YAML
sed -i "s#PLACEHOLDER#$work/runner3#" "$work/case3.yml"
if ! play "$work/case3.yml" "$work/case3.out"; then
  echo "FAIL case3: already-registered run failed (should skip register)"; cat "$work/case3.out"; fail=1
elif [[ -s "$FAKE_CONFIG_LOG" ]]; then
  echo "FAIL case3: config.sh ran for an already-registered runner"; fail=1
else echo "ok case3: already-registered skip"; fi

exit "$fail"
```

- [ ] **Step 4: Wire the harness into `just test-ansible`.**

Modify the `test-ansible` recipe in `justfile` to append a second line:
```make
test-ansible:
    uv run --with 'ansible-core==2.21.1' ./deploy/ansible/tests/run-gdbstub-acl-prune.sh
    uv run --with 'ansible-core==2.21.1' ./deploy/ansible/tests/run-github-runner-preflight.sh
```

- [ ] **Step 5: Run the harness and confirm the fail cases go red (role logic absent).**
```bash
chmod +x deploy/ansible/tests/run-github-runner-preflight.sh deploy/ansible/tests/fake-config-sh
just test-ansible
```
Expected: cases 1 (token fail-closed) and 2 (arch fail-loud) FAIL against the Task-2 placeholder — under `--tags github_runner_register` no tagged tasks exist yet, so neither expected-failure fires. Case 3 (already-registered skip) passes vacuously here (nothing runs, so `config.sh` is not called): it is a **regression guard** against future skip-logic breakage, not a red-first case; its failure-detecting power is demonstrated in Task 7 Step 3.

- [ ] **Step 6: Verify shellcheck + commit.**
```bash
just lint-shell
git add deploy/ansible/tests/github_runner_preflight.yml deploy/ansible/tests/run-github-runner-preflight.sh \
        deploy/ansible/tests/fake-config-sh justfile
git commit -m "test(1291): failing github_runner preflight regression harness"
```

---

## Task 7: `github_runner` — arch resolve, download, idempotence, register, service, liveness

**Where it fits:** Implements the role to turn Task 6's harness green.

**Files:**
- Modify: `deploy/ansible/roles/github_runner/tasks/main.yml`

**Interfaces:**
- Consumes: all `github_runner_*` defaults (Task 1) + `install_staging_dir`/uid facts.
- Produces: an installed (stopped) runner service; tags `github_runner_register` on the download+register block.

- [ ] **Step 1: Replace the placeholder with the full task file.**
```yaml
---
# The set_fact + marker stat are tagged github_runner_register too, so a `--tags
# github_runner_register` run (the regression harness) evaluates the skip decision; the
# svc.sh / .env / systemd tasks below are UNtagged, so the harness never reaches them.
- name: Resolve the runner asset + label for this arch
  ansible.builtin.set_fact:
    github_runner_asset: "{{ github_runner_arch_map[ansible_architecture].asset | default('') }}"
    github_runner_label_token: "{{ github_runner_arch_map[ansible_architecture].label | default('') }}"
  tags: [github_runner_register]

- name: Detect an existing registration (idempotence + liveness guard)
  ansible.builtin.stat:
    path: "{{ github_runner_install_dir }}/.runner"
  register: github_runner_marker
  tags: [github_runner_register]

- name: Register the runner (first-time branch)
  when: not github_runner_marker.stat.exists
  tags: [github_runner_register]
  block:
    - name: Fail closed when no registration token is supplied
      ansible.builtin.assert:
        that: github_runner_registration_token | length > 0
        fail_msg: "github_runner_registration_token is required to register a new runner"
      no_log: true

    - name: Fail loud when this arch has no runner asset and no override URL
      ansible.builtin.fail:
        msg: >-
          No actions/runner asset for {{ ansible_architecture }} and github_runner_tarball_url
          is unset. Upstream ships no ppc64le asset; build one and set github_runner_tarball_url.
      when:
        - github_runner_asset | length == 0
        - github_runner_tarball_url | length == 0

    - name: Resolve the download URL
      ansible.builtin.set_fact:
        github_runner_url: >-
          {{ github_runner_tarball_url if github_runner_tarball_url | length > 0 else
             'https://github.com/actions/runner/releases/download/v' ~ github_runner_version ~
             '/actions-runner-linux-' ~ github_runner_asset ~ '-' ~ github_runner_version ~ '.tar.gz' }}

    - name: Create the runner install dir
      ansible.builtin.file:
        path: "{{ github_runner_install_dir }}"
        state: directory
        owner: "{{ github_runner_user }}"
        group: "{{ github_runner_user }}"
        mode: "0755"

    - name: Download + checksum-verify the runner tarball (operator-pinned sha256)
      # ALWAYS download (the next task unarchives it). Only the checksum is conditional: a
      # pinned sha256 is enforced when set; an override tarball with no pin uses `omit` (a
      # discouraged path the runbook flags — pin the override sha too when possible).
      ansible.builtin.get_url:
        url: "{{ github_runner_url }}"
        dest: "{{ github_runner_install_dir }}/runner.tar.gz"
        checksum: >-
          {{ ('sha256:' ~ github_runner_sha256)
             if github_runner_sha256 not in ['', 'SET-AT-IMPLEMENTATION'] else omit }}
        owner: "{{ github_runner_user }}"
        mode: "0644"

    - name: Extract the runner
      ansible.builtin.unarchive:
        src: "{{ github_runner_install_dir }}/runner.tar.gz"
        dest: "{{ github_runner_install_dir }}"
        remote_src: true
        owner: "{{ github_runner_user }}"

    - name: Configure (register) the runner
      ansible.builtin.command:
        cmd: >-
          ./config.sh --unattended --replace
          --url {{ github_runner_repo_url }}
          --token {{ github_runner_registration_token }}
          --labels {{ (github_runner_extra_labels + [github_runner_label_token]) | join(',') }}
        chdir: "{{ github_runner_install_dir }}"
        creates: "{{ github_runner_install_dir }}/.runner"
      become_user: "{{ github_runner_user }}"
      no_log: true

- name: Set XDG_RUNTIME_DIR + KDIVE_SECRETS_ROOT for the runner job process
  # actions-runner loads <install_dir>/.env into every job process, so this is where the short
  # QMP-socket base (#1258) + the S3 secrets pointer reach the test process. getent_passwd was
  # populated by live_vm_host earlier in the play (and again here if github_runner runs alone).
  ansible.builtin.getent:
    database: passwd
    key: "{{ github_runner_user }}"

- name: Write the runner .env
  ansible.builtin.copy:
    dest: "{{ github_runner_install_dir }}/.env"
    content: |
      XDG_RUNTIME_DIR=/run/user/{{ ansible_facts.getent_passwd[github_runner_user][1] }}
      KDIVE_SECRETS_ROOT={{ github_runner_secrets_root }}
      KDIVE_PYTHON={{ live_vm_venv }}/.venv/bin/python
    owner: "{{ github_runner_user }}"
    mode: "0640"

- name: Assert the runner .env carries the short XDG_RUNTIME_DIR (#1258 provisioning check)
  ansible.builtin.command: "grep -q '^XDG_RUNTIME_DIR=/run/user/' {{ github_runner_install_dir }}/.env"
  changed_when: false

- name: Install the runner service (as the service account)
  # svc.sh writes the generated unit name into <install_dir>/.service; guard the install on
  # that marker (the unit name is actions.runner.<owner>-<repo>.<runner>.service — NOT derivable
  # from the URL basename, so do not hard-code it).
  ansible.builtin.command:
    cmd: "./svc.sh install {{ github_runner_user }}"
    chdir: "{{ github_runner_install_dir }}"
    creates: "{{ github_runner_install_dir }}/.service"
  when: github_runner_marker.stat.exists or github_runner_registration_token | length > 0

- name: Read the generated systemd unit name
  ansible.builtin.slurp:
    src: "{{ github_runner_install_dir }}/.service"
  register: github_runner_unit_file

- name: Enable + start the runner service only when the trust posture is confirmed
  ansible.builtin.systemd_service:
    name: "{{ (github_runner_unit_file.content | b64decode).strip() }}"
    enabled: "{{ github_runner_service_enabled }}"
    state: "{{ 'started' if github_runner_service_enabled else 'stopped' }}"
  when: github_runner_service_enabled | bool
```

> Implementer note: the liveness/re-register-on-stale check when enabling (spec's stale-registration guard) is a follow-up refinement — for this task, ship the marker-based idempotence + install-stopped; the runbook (Task 8) notes that enabling after a long stop should confirm the runner shows online, with `config.sh remove` + re-run as recovery. Keep the role lint-clean; a full `config.sh --check`/API liveness probe can be a small follow-up if `just test-ansible` and review call for it.

- [ ] **Step 2: Run the harness — expect all three cases PASS.**
```bash
just test-ansible
```
Expected: `ok case1 / ok case2 / ok case3`, exit 0.

- [ ] **Step 3: Mutation-check the harness catches regressions (both fail paths + the skip).**
  - Delete the `assert` "Fail closed when no registration token" task → `just test-ansible` → confirm case1 FAILS → restore.
  - Break the block guard `when: not github_runner_marker.stat.exists` (e.g. change to `when: true`) → `just test-ansible` → confirm **case3 FAILS** (the register block now runs for an already-registered runner and calls the fake `config.sh`, which case3 asserts must not happen) → restore.
  This demonstrates case3's skip logic has real failure-detecting coverage, not just a vacuous pass. (Repo test philosophy: verify the test catches the failure.)

- [ ] **Step 4: Verify lint + syntax.**
```bash
just lint-ansible
ANSIBLE_CONFIG=deploy/ansible/ansible.cfg uv run --with 'ansible-core==2.21.1' \
  ansible-playbook -i deploy/ansible/inventory/hosts.yml deploy/ansible/playbooks/runner.yml --syntax-check
```
Expected: no errors.

- [ ] **Step 5: Commit.**
```bash
git add deploy/ansible/roles/github_runner/tasks/main.yml
git commit -m "feat(1291): github_runner arch-resolve, checksum download, register, install-stopped"
```

---

## Task 8: Runbook + discoverability pointers

**Where it fits:** The "stop the relearning" deliverable — the operator-facing bring-up guide and the pointers that make it discoverable.

**Files:**
- Create: `self-hosted-kvm-runner.md` under `docs/operating/runbooks/`
- Modify: `deploy/ansible/README.md`
- Modify: `AGENTS.md`

**Interfaces:** none (docs).

- [ ] **Step 1: Write the runbook** (`self-hosted-kvm-runner.md` under `docs/operating/runbooks/`) covering, in order: prerequisites (Rocky 10, `just`/`uv`, collections via `ansible-galaxy install -r requirements.yml`); the `ansible-playbook playbooks/runner.yml` command and what each role does; the persistent venv + the `KDIVE_PYTHON` contract sub-issue D reuses (do not rebuild per job); obtaining a registration token; **the ordered security steps — apply the repo "Require approval for all outside collaborators" setting and D's `if:` guard BEFORE setting `github_runner_service_enabled: true`**; the offline-removal warning (leaving the service stopped past GitHub's ~14-day window invalidates the registration; recovery is `config.sh remove` then re-run with a fresh token); wiring `KDIVE_S3_*` repo/org secrets and where the credential material lands (C/D/operator, since A's resolver does not check it); the ppc64le `github_runner_tarball_url` override; re-applying the `/boot` chmod after a kernel upgrade; deregistration; and the verification steps (idempotence `0 changed` on an already-registered host + off-host `check-local-libvirt`). Follow the operator-doc convention: use `ansible-playbook`/`scripts/*.sh`, not `just`, in the walkthrough. Keep prose plain (no banned words).

- [ ] **Step 2: Add a runner section to `deploy/ansible/README.md`** — a short subsection under Layout/Usage pointing at `playbooks/runner.yml`, the two new roles, and the runbook, noting it is the local-libvirt CI-runner path (distinct from the remote-libvirt provider bring-up).

- [ ] **Step 3: Add a one-line pointer to `AGENTS.md`** in the `live_vm` conventions area, e.g. after the three-live-tiers note: "The self-hosted KVM runner host is codified in `deploy/ansible/playbooks/runner.yml` + the `self-hosted-kvm-runner.md` runbook under `docs/operating/runbooks/` (ADR-0387, #1291)."

- [ ] **Step 4: Verify doc guards.**
```bash
just docs-links
just docs-paths
just adr-status-check
```
Expected: all green (the runbook now exists, so the spec's reference resolves).

- [ ] **Step 5: Commit.**
```bash
git add docs/operating/runbooks/self-hosted-kvm-runner.md deploy/ansible/README.md AGENTS.md
git commit -m "docs(1291): self-hosted KVM runner runbook + discoverability pointers"
```

---

## Task 9: Full guardrail run-through + local host-contract validation

**Where it fits:** The pre-ship verification the spec's "codify + validate host contract locally" acceptance calls for.

**Files:** none (verification only; fix-forward if a guard fails).

- [ ] **Step 1: Run the full PR-gated guardrail set.**
```bash
just lint-ansible && just test-ansible && just lint-shell && just docs-links && just docs-paths && just adr-status-check
prek run --all-files
```
Expected: all green. Fix any finding and re-run before proceeding.

- [ ] **Step 2: Local host-contract validation (non-invasive portion).** On this dev KVM host, confirm the contract *script* the gate leans on is green, and the new playbook is applicable:
```bash
./scripts/check-local-libvirt.sh; echo "exit=$?"
ANSIBLE_CONFIG=deploy/ansible/ansible.cfg uv run --with 'ansible-core==2.21.1' \
  ansible-playbook -i deploy/ansible/inventory/hosts.yml deploy/ansible/playbooks/runner.yml \
  --syntax-check
```
Expected: `check-local-libvirt.sh` reports the host ready (exit 0) or names concrete FAILs to note; `--syntax-check` passes. A full `ansible-playbook runner.yml` apply (which installs packages, chmods `/boot`, registers a runner) is an **operator step in the runbook**, not part of this branch — record in the PR description that live registration was validated by the harness + syntax-check + `check-local-libvirt`, with full apply deferred to the operator (matching how `deploy/ansible` ships codified-but-operator-run paths).

- [ ] **Step 3: No commit** unless Step 1 fixed a guard finding (then commit that fix with an imperative subject).

---

## Self-Review (completed against the spec)

- **Spec coverage:** reuse boundary (Task 1 playbook) · single service account (Tasks 1–7 use `github_runner_user`) · toolchain (Task 2) · /boot readability (Task 2) · venv + ABI match + KDIVE_PYTHON contract (Task 3) · both staging dirs + virt_image_t (Task 4) · two-part gate incl. both-dir label + group membership + XDG dir (Task 5) · arch fail-loud + tarball override seam (Task 7) · pinned checksum (Task 7) · idempotence marker guard + token fail-closed + install-stopped (Task 7) · service XDG/secrets env (Task 7) · regression harness for the three behaviors (Tasks 6–7) · runbook + pointers + trusted-events ordering + offline-window recovery + credential-ownership note (Task 8) · local validation (Task 9). The stale-registration *liveness* probe is scoped as a Task 7 follow-up note (spec allows the marker guard + runbook recovery as the floor).
- **Placeholders:** none — every code step carries actual YAML/Bash; the `getent` uid lookups are shown explicitly (verify.yml + github_runner), not left as prose.
- **Type/name consistency:** `github_runner_user`, `live_vm_staging_dir`, `install_staging_dir`, `live_vm_venv`, `github_runner_arch_map`, `github_runner_install_dir`, tag `github_runner_register` are used identically across tasks.
- **Plan-review fixes folded in:** harness isolates the register branch via `--tags github_runner_register` (so case 3 never reaches `svc.sh`); the label gate reads `ls -Zd` (not the inverted `matchpathcon -V`); `/boot` uses find+file for 0-changed idempotence; the venv root is pre-created + `uv` installed before the clone; the runner tarball always downloads (only the checksum is conditional); and Task 5 exercises the corrected `ls -Zd`/`getent` mechanisms in-branch.
