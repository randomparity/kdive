# Ansible remote-libvirt host bring-up — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build idempotent Ansible roles that take a bare Linux host (Ubuntu 26.04 / Fedora 44 / RHEL-Rocky 10, x86_64 or ppc64le) to a kdive remote-libvirt provider host — virtualization stack, `qemu+tls` mutual TLS, storage pool/network, gdbstub ACL, operator-staged base image — and emit the controller-side `systems.toml` block, automating the manual runbook `docs/operating/runbooks/remote-libvirt-host-setup.md` steps 1–6.

**Architecture:** Composable roles, one per runbook step, orchestrated by a thin `site.yml`, plus a controller-side PKI playbook and an opt-in image-build playbook. All distro/arch divergence is pushed into variables. Daemon model is **modular-only via an explicit switch-to-modular** on every distro. Security driver, kernel debuginfo, and crashkernel are parameterized and default off. The `remote_libvirt_facts` role renders the `systems.toml` block controller-side; the worker client-cert bundle is born controller-side in the PKI play and is never fetched back from a host.

**Tech Stack:** Ansible (collections: `community.crypto`, `community.general`, `ansible.posix`, `community.libvirt`); `virt-builder`/`virt-customize` (libguestfs); `firewalld`/`ufw`; pytest (structural template test, mirroring `tests/deploy/`); `yamllint` + `ansible-lint` gate wired into `just lint-ansible`, pre-commit, and `ci.yml`.

## Global Constraints

- Spec of record: `docs/superpowers/specs/2026-06-18-ansible-remote-libvirt-host-setup-design.md`. Ground truth for every command: `docs/operating/runbooks/remote-libvirt-host-setup.md` (steps 1–8).
- All new Ansible content lives under `deploy/ansible/` (mirrors the `deploy/*/README.md` convention).
- `disable_security_driver` defaults **false** (opt-in per host). `include_kernel_debuginfo` defaults **false**. Image is lean by default (provision/`host_dump` path).
- Daemon model is **modular-only** via explicit switch-to-modular on every distro: mask all `libvirtd*` units, enable the modular socket set. `virtproxyd-tls.socket` owns the `:16514` listener — never a `libvirtd*` socket.
- No host→controller `fetch` of the client bundle. The host only ever receives its server cert + the CA cert.
- Read-only / assert / command tasks set `changed_when:` honestly so the "second run = 0 changed" idempotence bar measures real drift.
- The lint gate has **three** wiring points that ship together (CI runs `just lint`/`type`/`test` separately and never `just ci`): the `lint-ansible` recipe in `justfile`, a `.pre-commit-config.yaml` hook, and an explicit step in `.github/workflows/ci.yml`.
- The emitted `systems.toml` block matches `systems.toml.example` field-for-field. `[[remote_libvirt]]` requires `vcpus` and `memory_mb` (billable ceiling).
- Secrets: CA private key + worker client key are written to the controller artifacts dir, **ansible-vault-encrypted**, never committed; the artifacts dir is gitignored.
- ppc64le paths are implemented but flagged **unvalidated** (no ppc64le test host).
- All shell the roles drop onto hosts must respect the repo's bash policy where applicable, but Ansible task YAML is the primary artifact; prefer modules over `command`/`shell`, and where a `command` is unavoidable set `changed_when`/`failed_when` explicitly.

---

## File Structure

**Created under `deploy/ansible/`:**

```
deploy/ansible/
  README.md
  ansible.cfg
  requirements.yml
  .yamllint
  .ansible-lint
  inventory/
    hosts.yml
    group_vars/all.yml
    host_vars/ub26-big.yml
    host_vars/fed44-big.yml
  site.yml
  playbooks/
    pki.yml
    image.yml
  roles/
    libvirt_stack/{defaults/main.yml, tasks/main.yml}
    libvirt_tls/{defaults/main.yml, tasks/main.yml, handlers/main.yml, templates/virtproxyd.conf.j2}
    libvirt_pool_net/{tasks/main.yml}
    guest_image_prereqs/{tasks/main.yml}
    guest_base_image/{defaults/main.yml, tasks/main.yml}
    gdbstub_acl/{defaults/main.yml, tasks/main.yml}
    remote_libvirt_facts/{defaults/main.yml, tasks/main.yml, templates/systems_toml_block.j2}
```

**Modified at repo root:**
- `justfile` — add `lint-ansible` recipe (after `lint-shell`, ~`justfile:144`).
- `.pre-commit-config.yaml` — add a `local` `ansible-lint` hook.
- `.github/workflows/ci.yml` — add a `Lint Ansible` step after the `Lint shell scripts` step.
- `.gitignore` — ignore `deploy/ansible/artifacts/`.

**Created tests:**
- `tests/deploy/test_remote_libvirt_facts.py` — structural test of the rendered block template (read-text + assert, mirroring `test_systemd_units.py`).

**Build order keeps the project always-lintable:** the lint gate plus the leaf `remote_libvirt_facts` role land first (Task 1), and each later role is added to `site.yml`/`image.yml` only in its own task, so `ansible-playbook --syntax-check` and `ansible-lint` stay green at every commit.

---

## Task 1: Project skeleton, lint gate, and `remote_libvirt_facts` leaf role

**Files:**
- Create: `deploy/ansible/ansible.cfg`
- Create: `deploy/ansible/requirements.yml`
- Create: `deploy/ansible/.yamllint`
- Create: `deploy/ansible/.ansible-lint`
- Create: `deploy/ansible/inventory/hosts.yml`
- Create: `deploy/ansible/inventory/group_vars/all.yml`
- Create: `deploy/ansible/inventory/host_vars/ub26-big.yml`
- Create: `deploy/ansible/inventory/host_vars/fed44-big.yml`
- Create: `deploy/ansible/roles/remote_libvirt_facts/defaults/main.yml`
- Create: `deploy/ansible/roles/remote_libvirt_facts/tasks/main.yml`
- Create: `deploy/ansible/roles/remote_libvirt_facts/templates/systems_toml_block.j2`
- Create: `deploy/ansible/site.yml`
- Create: `tests/deploy/test_remote_libvirt_facts.py`
- Modify: `justfile` (add `lint-ansible` recipe)
- Modify: `.pre-commit-config.yaml` (add ansible-lint local hook)
- Modify: `.github/workflows/ci.yml` (add Lint Ansible step)
- Modify: `.gitignore` (ignore artifacts dir)

**Interfaces:**
- Produces: the `deploy/ansible/` project root, the `remote_libvirt_hosts` inventory group, `group_vars/all.yml` config surface (consumed by every later role), and `just lint-ansible` (run at the end of every later task).
- Produces: `roles/remote_libvirt_facts` rendering `{{ pki_artifacts_dir }}/<host>-systems.toml` from `templates/systems_toml_block.j2`.

- [ ] **Step 1: Write the failing structural test**

Create `tests/deploy/test_remote_libvirt_facts.py`:

```python
"""Structural checks on the rendered systems.toml block template.

Mirrors tests/deploy/test_systemd_units.py: read the Jinja2 source as text and
assert every required field token is present, so the test needs no ansible or
jinja2 runtime. The field set is locked to systems.toml.example (schema v2).
"""

from __future__ import annotations

from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "ansible"
    / "roles"
    / "remote_libvirt_facts"
    / "templates"
    / "systems_toml_block.j2"
)

REMOTE_LIBVIRT_FIELDS = (
    "name",
    "uri",
    "gdb_addr",
    "gdbstub_range",
    "client_cert_ref",
    "client_key_ref",
    "ca_cert_ref",
    "base_image",
    "cost_class",
    "concurrent_allocation_cap",
    "vcpus",
    "memory_mb",
    "shapes",
)

IMAGE_FIELDS = (
    "provider",
    "arch",
    "format",
    "root_device",
    "visibility",
)


def test_template_has_both_blocks() -> None:
    text = TEMPLATE.read_text()
    assert "[[remote_libvirt]]" in text
    assert "[[image]]" in text
    assert 'kind = "staged"' in text
    assert "volume" in text


def test_remote_libvirt_block_has_all_fields() -> None:
    text = TEMPLATE.read_text()
    for field in REMOTE_LIBVIRT_FIELDS:
        assert f"{field} =" in text, f"missing remote_libvirt field: {field}"


def test_image_block_has_all_fields() -> None:
    text = TEMPLATE.read_text()
    for field in IMAGE_FIELDS:
        assert f"{field} =" in text, f"missing image field: {field}"


def test_no_host_to_controller_fetch_marker() -> None:
    # The facts template must never reference a fetched client bundle path.
    text = TEMPLATE.read_text()
    assert "fetch" not in text.lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m pytest tests/deploy/test_remote_libvirt_facts.py -q`
Expected: FAIL — `FileNotFoundError` (template does not exist yet).

- [ ] **Step 3: Create the facts template**

Create `deploy/ansible/roles/remote_libvirt_facts/templates/systems_toml_block.j2`:

```jinja
# Rendered by the remote_libvirt_facts role for {{ inventory_hostname }}.
# Paste ONE host's [[remote_libvirt]] block into the deployment's systems.toml
# (the reconciler rejects multiple [[remote_libvirt]] blocks — singleton until
# multi-instance remote selection lands). The matching [[image]] block is safe to
# include alongside it. Machine type is an env knob, not a systems.toml field:
#   KDIVE_REMOTE_LIBVIRT_MACHINE={{ machine_type[ansible_architecture] }}

[[image]]
provider = "remote-libvirt"
name = "{{ base_image_name }}"
arch = "{{ ansible_architecture }}"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "{{ base_image_name }}.qcow2"

[[remote_libvirt]]
name = "{{ inventory_hostname }}"
uri = "qemu+tls://{{ remote_host_fqdn }}/system"
gdb_addr = "{{ gdb_addr }}"
gdbstub_range = "{{ gdbstub_range }}"
client_cert_ref = "{{ client_cert_ref }}"
client_key_ref = "{{ client_key_ref }}"   # pragma: allowlist secret - filename ref
ca_cert_ref = "{{ ca_cert_ref }}"
base_image = "{{ base_image_name }}"
cost_class = "{{ cost_class }}"
concurrent_allocation_cap = {{ concurrent_allocation_cap }}
vcpus = {{ vcpus }}
memory_mb = {{ memory_mb }}
shapes = [{% for s in shapes %}"{{ s }}"{% if not loop.last %}, {% endif %}{% endfor %}]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run python -m pytest tests/deploy/test_remote_libvirt_facts.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Create the facts role defaults and tasks**

Create `deploy/ansible/roles/remote_libvirt_facts/defaults/main.yml`:

```yaml
---
# Secret-ref filenames the SecretRegistry resolves (never secret material).
ca_cert_ref: cacert.pem
client_cert_ref: clientcert.pem
client_key_ref: clientkey.pem  # pragma: allowlist secret - filename ref
```

Create `deploy/ansible/roles/remote_libvirt_facts/tasks/main.yml`:

```yaml
---
- name: Ensure the controller artifacts dir exists
  ansible.builtin.file:
    path: "{{ pki_artifacts_dir }}"
    state: directory
    mode: "0750"
  delegate_to: localhost
  run_once: false

- name: Render the systems.toml block for this host (controller-side)
  ansible.builtin.template:
    src: systems_toml_block.j2
    dest: "{{ pki_artifacts_dir }}/{{ inventory_hostname }}-systems.toml"
    mode: "0640"
  delegate_to: localhost
```

- [ ] **Step 6: Create the inventory, group_vars, and host_vars**

Create `deploy/ansible/inventory/hosts.yml`:

```yaml
---
all:
  children:
    remote_libvirt_hosts:
      hosts:
        ub26-big:
        fed44-big:
```

Create `deploy/ansible/inventory/group_vars/all.yml`:

```yaml
---
# --- storage / network (KDIVE_REMOTE_LIBVIRT_{STORAGE_POOL,NETWORK} aligned) ---
storage_pool_target: /var/lib/libvirt/images
libvirt_network: default

# --- gdbstub ACL ---
worker_cidr: "192.168.16.0/24"
gdbstub_range: "47000:47099"
gdb_addr: ""  # REQUIRED per host; provisioning fails closed without it (set in host_vars)

# --- base image ---
base_image_distro: fedora
base_image_version: "43"
base_image_name: "fedora-kdive-remote-base-{{ base_image_version }}"
base_image_source: virt-builder  # virt-builder | cloud-image
helper_src: "{{ playbook_dir }}/../remote-libvirt-guest-helpers"
build_packages:
  - qemu-guest-agent
  - drgn
  - kexec-tools
  - makedumpfile
  - kdump-utils
  - curl
  - tar
  - openssl
  - python3
include_kernel_debuginfo: false
crashkernel_value: "256M"  # applied only when include_kernel_debuginfo | bool
force_image_rebuild: false

# --- billable ceiling (required by the [[remote_libvirt]] schema) ---
vcpus: 16
memory_mb: 65536
concurrent_allocation_cap: 1
shapes: ["small", "medium", "large", "max"]
cost_class: remote

# --- security driver (opt-in; default keeps SELinux/AppArmor on) ---
disable_security_driver: false

# --- PKI ---
pki_mode: generate  # generate | byo
pki_artifacts_dir: "{{ playbook_dir }}/artifacts"

# --- machine type by arch (env knob KDIVE_REMOTE_LIBVIRT_MACHINE) ---
machine_type:
  x86_64: pc
  ppc64le: pseries
```

Create `deploy/ansible/inventory/host_vars/ub26-big.yml`:

```yaml
---
remote_host_fqdn: ub26-big.example
gdb_addr: 192.168.10.20
```

Create `deploy/ansible/inventory/host_vars/fed44-big.yml`:

```yaml
---
remote_host_fqdn: fed44-big.example
gdb_addr: 192.168.10.21
```

- [ ] **Step 7: Create `ansible.cfg`, `requirements.yml`, and lint configs**

Create `deploy/ansible/ansible.cfg`:

```ini
[defaults]
inventory = inventory/hosts.yml
roles_path = roles
host_key_checking = True
stdout_callback = yaml
nocows = True

[ssh_connection]
pipelining = True
```

Create `deploy/ansible/requirements.yml` (look up current stable versions when executing; do not assume from memory):

```yaml
---
collections:
  - name: community.crypto
    version: ">=2.0.0"
  - name: community.general
    version: ">=8.0.0"
  - name: ansible.posix
    version: ">=1.5.0"
  - name: community.libvirt
    version: ">=1.3.0"
```

Create `deploy/ansible/.yamllint`:

```yaml
---
extends: default
rules:
  line-length:
    max: 160
  truthy:
    allowed-values: ["true", "false"]
  comments:
    min-spaces-from-content: 1
  document-start: disable
```

Create `deploy/ansible/.ansible-lint`:

```yaml
---
profile: production
exclude_paths:
  - artifacts/
```

Create `deploy/ansible/site.yml` (only the leaf role exists so far; later tasks prepend roles in order):

```yaml
---
- name: Emit remote-libvirt facts (controller-side)
  hosts: remote_libvirt_hosts
  gather_facts: true
  roles:
    - remote_libvirt_facts
```

- [ ] **Step 8: Wire the lint gate (recipe + pre-commit + ci.yml)**

Add to `justfile` immediately after the `lint-shell` recipe (the block ending at the `lint-workflows` comment, ~`justfile:144`):

```just
# Lint and syntax-check the Ansible automation (deploy/ansible).
lint-ansible:
    uv run --with 'ansible-lint==24.12.2' --with 'ansible-core>=2.17' \
        yamllint -c deploy/ansible/.yamllint deploy/ansible
    uv run --with 'ansible-lint==24.12.2' --with 'ansible-core>=2.17' \
        ansible-lint -c deploy/ansible/.ansible-lint deploy/ansible
```

(When executing, confirm the current stable `ansible-lint`/`ansible-core` versions and pin exact `==`.)

Add to `.pre-commit-config.yaml` under the existing `repo: local` `hooks:` list (after the `ty` hook):

```yaml
      - id: lint-ansible
        name: lint-ansible
        # Delegates to the justfile recipe so there is one definition shared with CI.
        entry: just lint-ansible
        language: system
        files: ^deploy/ansible/
        pass_filenames: false
```

Add to `.github/workflows/ci.yml` immediately after the `Lint shell scripts` step (`just lint-shell`, ~`ci.yml:83`):

```yaml
      - name: Lint Ansible
        # CI invokes recipes individually (never `just ci`), so list this explicitly to gate PRs.
        run: just lint-ansible
```

Add to `.gitignore` (root):

```gitignore
deploy/ansible/artifacts/
```

- [ ] **Step 9: Run the lint gate and syntax-check**

Run: `just lint-ansible`
Expected: yamllint clean; ansible-lint reports no errors on the skeleton + facts role.

Run (from `deploy/ansible/`): `uv run --with 'ansible-core>=2.17' ansible-playbook site.yml --syntax-check -i inventory/hosts.yml`
Expected: `playbook: site.yml` (syntax OK).

- [ ] **Step 10: Run the structural test and commit**

Run: `uv run python -m pytest tests/deploy/test_remote_libvirt_facts.py -q`
Expected: PASS.

```bash
git add deploy/ansible tests/deploy/test_remote_libvirt_facts.py justfile \
  .pre-commit-config.yaml .github/workflows/ci.yml .gitignore
git commit -m "feat(ansible): scaffold remote-libvirt project, lint gate, and facts role"
```

---

## Task 2: `libvirt_stack` role (switch-to-modular)

**Files:**
- Create: `deploy/ansible/roles/libvirt_stack/defaults/main.yml`
- Create: `deploy/ansible/roles/libvirt_stack/tasks/main.yml`
- Modify: `deploy/ansible/site.yml` (prepend the role as the first play)

**Interfaces:**
- Consumes: `group_vars/all.yml`.
- Produces: a host running the modular daemons (qemu:///system served by `virtqemud`), all `libvirtd*` units masked, the login user in `kvm`/`libvirt`, KVM validated. Later roles assume the modular stack is up.

- [ ] **Step 1: Create the role defaults**

Create `deploy/ansible/roles/libvirt_stack/defaults/main.yml`:

```yaml
---
libvirt_packages_debian:
  - libvirt-daemon-system
  - libvirt-clients
  - qemu-utils
  - libguestfs-tools
  - virtinst
  - gnutls-bin
libvirt_packages_redhat:
  - libvirt
  - libvirt-client
  - qemu-img
  - libguestfs-tools-c
  - virt-install
  - gnutls-utils

# qemu emulator package by os_family + arch.
qemu_package_map:
  Debian:
    x86_64: qemu-system-x86
    ppc64le: qemu-system-ppc
  RedHat:
    x86_64: qemu-kvm
    ppc64le: qemu-kvm

monolithic_units:
  - libvirtd.service
  - libvirtd.socket
  - libvirtd-ro.socket
  - libvirtd-admin.socket
  - libvirtd-tls.socket
  - libvirtd-tcp.socket

# NOTE: virtproxyd-tls.socket is enabled by the libvirt_tls role, not here.
modular_sockets:
  - virtqemud.socket
  - virtnetworkd.socket
  - virtstoraged.socket
  - virtnodedevd.socket
  - virtsecretd.socket
  - virtproxyd.socket
```

- [ ] **Step 2: Create the role tasks**

Create `deploy/ansible/roles/libvirt_stack/tasks/main.yml`:

```yaml
---
- name: Install virtualization packages (Debian)
  ansible.builtin.apt:
    name: "{{ libvirt_packages_debian + [qemu_package_map['Debian'][ansible_architecture]] }}"
    state: present
    update_cache: true
  when: ansible_os_family == 'Debian'

- name: Install virtualization packages (RedHat)
  ansible.builtin.dnf:
    name: "{{ libvirt_packages_redhat + [qemu_package_map['RedHat'][ansible_architecture]] }}"
    state: present
  when: ansible_os_family == 'RedHat'

# --- switch-to-modular: required even on Ubuntu (boots monolithic by default
# --- but ships the modular daemons in libvirt-daemon-system). ---
- name: Stop the monolithic libvirt units (best effort; may be absent)
  ansible.builtin.systemd_service:
    name: "{{ item }}"
    state: stopped
  loop: "{{ monolithic_units }}"
  failed_when: false

- name: Disable and mask the monolithic libvirt units
  ansible.builtin.systemd_service:
    name: "{{ item }}"
    enabled: false
    masked: true
  loop: "{{ monolithic_units }}"

- name: Enable and start the modular libvirt sockets
  ansible.builtin.systemd_service:
    name: "{{ item }}"
    enabled: true
    state: started
    masked: false
  loop: "{{ modular_sockets }}"

- name: Add the login user to the kvm and libvirt groups
  ansible.builtin.user:
    name: "{{ ansible_user_id }}"
    groups: [kvm, libvirt]
    append: true

- name: Run virt-host-validate (read-only)
  ansible.builtin.command: virt-host-validate qemu
  register: virt_host_validate
  changed_when: false
  failed_when: false

- name: Assert hardware virtualization and /dev/kvm pass
  ansible.builtin.assert:
    that:
      - virt_host_validate.stdout is search('hardware virtualization\\s*:\\s*PASS')
      - virt_host_validate.stdout is search('/dev/kvm exists\\s*:\\s*PASS')
    fail_msg: "KVM not usable on {{ inventory_hostname }}:\n{{ virt_host_validate.stdout }}"
    success_msg: "KVM acceleration and /dev/kvm present."
```

- [ ] **Step 3: Prepend the role to `site.yml`**

Edit `deploy/ansible/site.yml` to add a stack play **before** the facts play:

```yaml
---
- name: Install the virtualization stack (switch-to-modular)
  hosts: remote_libvirt_hosts
  become: true
  gather_facts: true
  roles:
    - libvirt_stack

- name: Emit remote-libvirt facts (controller-side)
  hosts: remote_libvirt_hosts
  gather_facts: true
  roles:
    - remote_libvirt_facts
```

- [ ] **Step 4: Syntax-check and lint**

Run (from `deploy/ansible/`): `uv run --with 'ansible-core>=2.17' ansible-playbook site.yml --syntax-check -i inventory/hosts.yml`
Expected: syntax OK.

Run: `just lint-ansible`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add deploy/ansible/roles/libvirt_stack deploy/ansible/site.yml
git commit -m "feat(ansible): libvirt_stack role with explicit switch-to-modular"
```

---

## Task 3: `libvirt_tls` role (mutual TLS + verification gate)

**Files:**
- Create: `deploy/ansible/roles/libvirt_tls/defaults/main.yml`
- Create: `deploy/ansible/roles/libvirt_tls/tasks/main.yml`
- Create: `deploy/ansible/roles/libvirt_tls/handlers/main.yml`
- Create: `deploy/ansible/roles/libvirt_tls/templates/virtproxyd.conf.j2`
- Modify: `deploy/ansible/site.yml` (insert after `libvirt_stack`, before facts)

**Interfaces:**
- Consumes: per-host server PKI written by the PKI play (Task 8) at `{{ pki_artifacts_dir }}/{{ inventory_hostname }}/{servercert.pem,serverkey.pem}` and `{{ pki_artifacts_dir }}/cacert.pem`. (Task 3 lands before Task 8; the role is syntax/lint-validated here and exercised end-to-end once PKI exists.)
- Produces: a `virtproxyd-tls.socket` listener on `:16514` with `auth_tls="none"`, optionally `security_driver="none"`.

- [ ] **Step 1: Create the role defaults**

Create `deploy/ansible/roles/libvirt_tls/defaults/main.yml`:

```yaml
---
tls_port: 16514
pki_ca_dir: /etc/pki/CA
pki_libvirt_dir: /etc/pki/libvirt
```

- [ ] **Step 2: Create the virtproxyd.conf template**

Create `deploy/ansible/roles/libvirt_tls/templates/virtproxyd.conf.j2`:

```jinja
# Managed by the libvirt_tls Ansible role. Mutual TLS, no SASL; verification stays on
# (no_verify is forbidden by the provider URI validation).
listen_tls = 1
listen_tcp = 0
auth_tls = "none"
```

- [ ] **Step 3: Create the handler**

Create `deploy/ansible/roles/libvirt_tls/handlers/main.yml`:

```yaml
---
- name: Restart virtproxyd
  ansible.builtin.systemd_service:
    name: virtproxyd.service
    state: restarted
```

- [ ] **Step 4: Create the role tasks**

Create `deploy/ansible/roles/libvirt_tls/tasks/main.yml`:

```yaml
---
- name: Create the PKI directories
  ansible.builtin.file:
    path: "{{ item.path }}"
    state: directory
    mode: "{{ item.mode }}"
  loop:
    - { path: "{{ pki_ca_dir }}", mode: "0755" }
    - { path: "{{ pki_libvirt_dir }}/private", mode: "0700" }

- name: Install the CA certificate
  ansible.builtin.copy:
    src: "{{ pki_artifacts_dir }}/cacert.pem"
    dest: "{{ pki_ca_dir }}/cacert.pem"
    mode: "0644"
  notify: Restart virtproxyd

- name: Install the server certificate
  ansible.builtin.copy:
    src: "{{ pki_artifacts_dir }}/{{ inventory_hostname }}/servercert.pem"
    dest: "{{ pki_libvirt_dir }}/servercert.pem"
    mode: "0644"
  notify: Restart virtproxyd

- name: Install the server private key
  ansible.builtin.copy:
    src: "{{ pki_artifacts_dir }}/{{ inventory_hostname }}/serverkey.pem"
    dest: "{{ pki_libvirt_dir }}/private/serverkey.pem"
    mode: "0600"
  notify: Restart virtproxyd

- name: Render /etc/libvirt/virtproxyd.conf
  ansible.builtin.template:
    src: virtproxyd.conf.j2
    dest: /etc/libvirt/virtproxyd.conf
    mode: "0600"
  notify: Restart virtproxyd

- name: Disable the libvirt security driver (opt-in only)
  ansible.builtin.lineinfile:
    path: /etc/libvirt/qemu.conf
    # Default keeps SELinux/AppArmor on; flip only when disable_security_driver is set,
    # and only after the narrow label fix (pool labeling / virt_use_* booleans) is tried.
    regexp: '^#*\s*security_driver\s*='
    line: 'security_driver = "none"'
  when: disable_security_driver | bool
  notify: Restart virtproxyd

- name: Enable and start virtproxyd-tls.socket
  ansible.builtin.systemd_service:
    name: virtproxyd-tls.socket
    enabled: true
    state: started
    masked: false
  notify: Restart virtproxyd

- name: Apply pending restarts before the verification gate
  ansible.builtin.meta: flush_handlers

# --- verification gate: :16514 LISTEN owned by virtproxyd-tls.socket ---
- name: Read the listener on the TLS port
  ansible.builtin.command: "ss -ltnp sport = :{{ tls_port }}"
  register: tls_listener
  changed_when: false

- name: Assert :16514 is LISTEN and owned by virtproxyd
  ansible.builtin.assert:
    that:
      - "'LISTEN' in tls_listener.stdout"
      - "'virtproxyd' in tls_listener.stdout"
      - "'libvirtd' not in tls_listener.stdout"
    fail_msg: >-
      :{{ tls_port }} is not owned by virtproxyd-tls.socket:
      {{ tls_listener.stdout }}
    success_msg: ":{{ tls_port }} LISTEN owned by virtproxyd-tls.socket."
```

- [ ] **Step 5: Insert the role into `site.yml`**

Add a TLS play after the stack play and before the facts play. The `become: true` plays can share one play with multiple roles; keep them separate for per-role isolation:

```yaml
- name: Configure mutual-TLS listener
  hosts: remote_libvirt_hosts
  become: true
  gather_facts: true
  roles:
    - libvirt_tls
```

(Place it between the `libvirt_stack` play and the `remote_libvirt_facts` play.)

- [ ] **Step 6: Syntax-check, lint, commit**

Run (from `deploy/ansible/`): `uv run --with 'ansible-core>=2.17' ansible-playbook site.yml --syntax-check -i inventory/hosts.yml`
Expected: syntax OK.

Run: `just lint-ansible`
Expected: clean.

```bash
git add deploy/ansible/roles/libvirt_tls deploy/ansible/site.yml
git commit -m "feat(ansible): libvirt_tls role with virtproxyd-tls verification gate"
```

---

## Task 4: `libvirt_pool_net` role

**Files:**
- Create: `deploy/ansible/roles/libvirt_pool_net/tasks/main.yml`
- Modify: `deploy/ansible/site.yml` (insert after `libvirt_tls`)

**Interfaces:**
- Consumes: `storage_pool_target`, `libvirt_network` from `group_vars/all.yml`.
- Produces: an autostarted `dir` storage pool and an autostarted `default` network, via `community.libvirt`.

- [ ] **Step 1: Create the role tasks**

Create `deploy/ansible/roles/libvirt_pool_net/tasks/main.yml`:

```yaml
---
- name: Define the storage pool
  community.libvirt.virt_pool:
    name: default
    command: define
    xml: |
      <pool type='dir'>
        <name>default</name>
        <target><path>{{ storage_pool_target }}</path></target>
      </pool>

- name: Build the storage pool
  community.libvirt.virt_pool:
    name: default
    state: active
    autostart: true

- name: Ensure the default network is active and autostarted
  community.libvirt.virt_net:
    name: "{{ libvirt_network }}"
    state: active
    autostart: true
```

- [ ] **Step 2: Insert into `site.yml`**

Add after the `libvirt_tls` play, before facts:

```yaml
- name: Define the storage pool and network
  hosts: remote_libvirt_hosts
  become: true
  gather_facts: true
  roles:
    - libvirt_pool_net
```

- [ ] **Step 3: Syntax-check, lint, commit**

Run (from `deploy/ansible/`): `uv run --with 'ansible-core>=2.17' ansible-playbook site.yml --syntax-check -i inventory/hosts.yml`
Expected: syntax OK.

Run: `just lint-ansible`
Expected: clean.

```bash
git add deploy/ansible/roles/libvirt_pool_net deploy/ansible/site.yml
git commit -m "feat(ansible): libvirt_pool_net role"
```

---

## Task 5: `gdbstub_acl` role (rich-rule ACL + negative verification)

**Files:**
- Create: `deploy/ansible/roles/gdbstub_acl/defaults/main.yml`
- Create: `deploy/ansible/roles/gdbstub_acl/tasks/main.yml`
- Modify: `deploy/ansible/site.yml` (insert after `libvirt_pool_net`, before facts)

**Interfaces:**
- Consumes: `worker_cidr`, `gdbstub_range`, `tls_port` (16514).
- Produces: firewalld rich-rule (Fedora/RHEL) or ufw (Ubuntu) ACL restricting `:16514` + the gdbstub range to `worker_cidr` with default-drop, plus an assertion that the generic drop rule is present (CI-able proxy for the off-CIDR refusal; the true off-CIDR test is an acceptance step in the README).

- [ ] **Step 1: Create the role defaults**

Create `deploy/ansible/roles/gdbstub_acl/defaults/main.yml`:

```yaml
---
acl_tls_port: 16514
# gdbstub_range is "47000:47099"; firewalld/ufw want a dash form for ranges.
gdbstub_port_range_dash: "{{ gdbstub_range | replace(':', '-') }}"
```

- [ ] **Step 2: Create the role tasks**

Create `deploy/ansible/roles/gdbstub_acl/tasks/main.yml`:

```yaml
---
# --- Fedora / RHEL: firewalld rich rules with explicit source address. A plain
# --- port-open would open the port to the whole zone; rich rules restrict the source. ---
- name: Configure firewalld rich-rule ACL (RedHat)
  when: ansible_os_family == 'RedHat'
  block:
    - name: Accept TLS + gdbstub from the worker CIDR
      ansible.posix.firewalld:
        rich_rule: >-
          rule family="ipv4" source address="{{ worker_cidr }}"
          port port="{{ item }}" protocol="tcp" accept
        permanent: true
        immediate: true
        state: enabled
      loop:
        - "{{ acl_tls_port }}"
        - "{{ gdbstub_port_range_dash }}"

    - name: Drop TLS + gdbstub from any other source (lower priority)
      ansible.posix.firewalld:
        rich_rule: >-
          rule priority="100" family="ipv4"
          port port="{{ item }}" protocol="tcp" drop
        permanent: true
        immediate: true
        state: enabled
      loop:
        - "{{ acl_tls_port }}"
        - "{{ gdbstub_port_range_dash }}"

    - name: Read back the rich rules (read-only)
      ansible.builtin.command: firewall-cmd --list-rich-rules
      register: firewalld_rules
      changed_when: false

    - name: Assert the default-drop rule is present (off-CIDR refused)
      ansible.builtin.assert:
        that:
          - firewalld_rules.stdout is search('port="' ~ acl_tls_port ~ '".*drop')
        fail_msg: "No default-drop rich rule for :{{ acl_tls_port }} — port is world-open."

# --- Ubuntu: ufw default-deny inbound is the drop; ordered allow-from rules open
# --- only the worker CIDR. ---
- name: Configure ufw ACL (Debian)
  when: ansible_os_family == 'Debian'
  block:
    - name: Allow TLS from the worker CIDR
      community.general.ufw:
        rule: allow
        direction: in
        proto: tcp
        from_ip: "{{ worker_cidr }}"
        to_port: "{{ acl_tls_port }}"

    - name: Allow gdbstub range from the worker CIDR
      community.general.ufw:
        rule: allow
        direction: in
        proto: tcp
        from_ip: "{{ worker_cidr }}"
        to_port: "{{ gdbstub_port_range_dash }}"

    - name: Set the default inbound policy to deny (the drop)
      community.general.ufw:
        default: deny
        direction: incoming
```

- [ ] **Step 3: Insert into `site.yml`**

Add after the `libvirt_pool_net` play, before facts:

```yaml
- name: Apply the gdbstub-port ACL
  hosts: remote_libvirt_hosts
  become: true
  gather_facts: true
  roles:
    - gdbstub_acl
```

- [ ] **Step 4: Syntax-check, lint, commit**

Run (from `deploy/ansible/`): `uv run --with 'ansible-core>=2.17' ansible-playbook site.yml --syntax-check -i inventory/hosts.yml`
Expected: syntax OK.

Run: `just lint-ansible`
Expected: clean.

```bash
git add deploy/ansible/roles/gdbstub_acl deploy/ansible/site.yml
git commit -m "feat(ansible): gdbstub_acl role with firewalld rich rules + negative check"
```

---

## Task 6: `guest_image_prereqs` role and `image.yml` playbook

**Files:**
- Create: `deploy/ansible/roles/guest_image_prereqs/tasks/main.yml`
- Create: `deploy/ansible/playbooks/image.yml`

**Interfaces:**
- Produces: the Ubuntu-only libguestfs appliance fixes; a separate `image.yml` playbook (the slow image path, kept out of `site.yml`).

- [ ] **Step 1: Create the role tasks**

Create `deploy/ansible/roles/guest_image_prereqs/tasks/main.yml` (all gated on Debian; no-op on Fedora/RHEL):

```yaml
---
- name: libguestfs appliance fixes
  when: ansible_os_family == 'Debian'
  block:
    - name: (a) The appliance must read the host kernel
      ansible.builtin.file:
        path: "/boot/vmlinuz-{{ ansible_kernel }}"
        mode: "0644"

    - name: (b1) Relax the global unprivileged-userns restriction for passt
      ansible.posix.sysctl:
        name: kernel.apparmor_restrict_unprivileged_userns
        value: "0"
        sysctl_file: /etc/sysctl.d/60-kdive-userns.conf
        state: present
        reload: true

    - name: (b2) Disable the per-binary AppArmor profile for passt
      ansible.builtin.file:
        src: /etc/apparmor.d/usr.bin.passt
        dest: /etc/apparmor.d/disable/usr.bin.passt
        state: link
        force: true
      register: passt_profile

    - name: (b2) Unload the passt AppArmor profile
      ansible.builtin.command: apparmor_parser -R /etc/apparmor.d/usr.bin.passt
      when: passt_profile.changed
      changed_when: passt_profile.changed
      failed_when: false

    - name: (c) Install a DHCP client for the appliance
      ansible.builtin.apt:
        name: isc-dhcp-client
        state: present
      register: dhcp_client

    - name: (c) Drop the stale supermin cache so it rebuilds with dhclient
      ansible.builtin.file:
        path: "{{ item }}"
        state: absent
      loop:
        - "/var/tmp/.guestfs-{{ ansible_user_uid }}"
        - "{{ ansible_env.HOME }}/.cache/libguestfs"
      when: dhcp_client.changed
```

- [ ] **Step 2: Create the `image.yml` playbook**

Create `deploy/ansible/playbooks/image.yml`:

```yaml
---
- name: Build the operator-staged base image (slow; opt-in)
  hosts: remote_libvirt_hosts
  become: true
  gather_facts: true
  roles:
    - guest_image_prereqs
```

(`guest_base_image` is appended in Task 7.)

- [ ] **Step 3: Syntax-check, lint, commit**

Run (from `deploy/ansible/`): `uv run --with 'ansible-core>=2.17' ansible-playbook playbooks/image.yml --syntax-check -i inventory/hosts.yml`
Expected: syntax OK.

Run: `just lint-ansible`
Expected: clean.

```bash
git add deploy/ansible/roles/guest_image_prereqs deploy/ansible/playbooks/image.yml
git commit -m "feat(ansible): guest_image_prereqs role and image.yml playbook"
```

---

## Task 7: `guest_base_image` role (native build + fallback, helpers, debuginfo/crashkernel)

**Files:**
- Create: `deploy/ansible/roles/guest_base_image/defaults/main.yml`
- Create: `deploy/ansible/roles/guest_base_image/tasks/main.yml`
- Modify: `deploy/ansible/playbooks/image.yml` (append the role)

**Interfaces:**
- Consumes: `base_image_source`, `base_image_version`, `base_image_name`, `build_packages`, `helper_src`, `include_kernel_debuginfo`, `crashkernel_value`, `force_image_rebuild`, `storage_pool_target`.
- Produces: a staged `{{ base_image_name }}.qcow2` volume in the pool carrying the guest agent, the three in-guest helpers (root-owned + relabeled), SELinux permissive, optionally `kernel-debuginfo` + crashkernel; emits its sha256.

- [ ] **Step 1: Create the role defaults**

Create `deploy/ansible/roles/guest_base_image/defaults/main.yml`:

```yaml
---
build_workdir: "{{ ansible_env.HOME }}/kdive-image-build"
helpers:
  - kdive-install-kernel
  - kdive-capture-vmcore
  - kdive-drgn
# Cloud-image fallback: a downloadable Fedora cloud qcow2 for the host arch.
# Set when base_image_source == 'cloud-image'; pin a real URL when executing.
cloud_image_url: "https://download.fedoraproject.org/pub/fedora/linux/releases/{{ base_image_version }}/Cloud/{{ ansible_architecture }}/images/Fedora-Cloud-Base-{{ base_image_version }}.qcow2"
```

- [ ] **Step 2: Create the role tasks**

Create `deploy/ansible/roles/guest_base_image/tasks/main.yml`:

```yaml
---
- name: Ensure the build working directory exists
  ansible.builtin.file:
    path: "{{ build_workdir }}"
    state: directory
    mode: "0755"

- name: Compute the package set (add kernel-debuginfo when requested)
  ansible.builtin.set_fact:
    image_packages: >-
      {{ build_packages + (['kernel-debuginfo'] if include_kernel_debuginfo | bool else []) }}

- name: Check whether the staged volume already exists
  ansible.builtin.stat:
    path: "{{ storage_pool_target }}/{{ base_image_name }}.qcow2"
  register: staged_volume

# --- Build path A: virt-builder (default), native on the host. ---
- name: Build the base image with virt-builder (native)
  ansible.builtin.command:
    argv:
      - virt-builder
      - "{{ base_image_distro }}-{{ base_image_version }}"
      - --arch
      - "{{ ansible_architecture }}"
      - --format
      - qcow2
      - --size
      - 10G
      - --output
      - "{{ build_workdir }}/{{ base_image_name }}.qcow2"
      - --install
      - "{{ image_packages | join(',') }}"
      - --run-command
      - systemctl enable qemu-guest-agent.service
      - --run-command
      - systemctl enable kdump.service
      - --run-command
      - sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config
  when:
    - base_image_source == 'virt-builder'
    - (not staged_volume.stat.exists) or (force_image_rebuild | bool)
  changed_when: true

# --- Build path B: cloud-image fallback (ppc64le when no virt-builder template). ---
- name: Download the Fedora cloud image (fallback)
  ansible.builtin.get_url:
    url: "{{ cloud_image_url }}"
    dest: "{{ build_workdir }}/{{ base_image_name }}.qcow2"
    mode: "0644"
  when:
    - base_image_source == 'cloud-image'
    - (not staged_volume.stat.exists) or (force_image_rebuild | bool)

- name: Customize the cloud image (same package/SELinux steps as virt-builder)
  ansible.builtin.command:
    argv:
      - virt-customize
      - -a
      - "{{ build_workdir }}/{{ base_image_name }}.qcow2"
      - --install
      - "{{ image_packages | join(',') }}"
      - --run-command
      - systemctl enable qemu-guest-agent.service
      - --run-command
      - systemctl enable kdump.service
      - --run-command
      - sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config
  when:
    - base_image_source == 'cloud-image'
    - (not staged_volume.stat.exists) or (force_image_rebuild | bool)
  changed_when: true

# --- Set the crashkernel reservation when debuginfo/kdump image is requested. ---
- name: Set the crashkernel reservation
  ansible.builtin.command:
    argv:
      - virt-customize
      - -a
      - "{{ build_workdir }}/{{ base_image_name }}.qcow2"
      - --run-command
      - "grubby --update-kernel=ALL --args=crashkernel={{ crashkernel_value }}"
  when:
    - include_kernel_debuginfo | bool
    - (not staged_volume.stat.exists) or (force_image_rebuild | bool)
  changed_when: true

# --- Install the three in-guest helpers: chown root:root + restorecon per helper
# --- (ENOENT-not-EACCES gotcha — chmod alone does not fix it). Build the argv list
# --- with a fact loop, then run virt-customize once. ---
- name: Build the virt-customize helper arguments
  ansible.builtin.set_fact:
    helper_args: >-
      {{ helper_args | default([])
         + ['--copy-in', helper_src ~ '/' ~ item ~ ':/usr/local/sbin/',
            '--run-command', 'chown root:root /usr/local/sbin/' ~ item,
            '--run-command', 'chmod 0755 /usr/local/sbin/' ~ item,
            '--run-command', 'restorecon -v /usr/local/sbin/' ~ item] }}
  loop: "{{ helpers }}"

- name: Install the in-guest helpers into the image
  ansible.builtin.command:
    argv: >-
      {{ ['virt-customize', '-a',
          build_workdir ~ '/' ~ base_image_name ~ '.qcow2'] + helper_args }}
  when: (not staged_volume.stat.exists) or (force_image_rebuild | bool)
  changed_when: true
```

Continue the tasks file:

```yaml
- name: Stage the finished image into the (root-owned) pool
  ansible.builtin.copy:
    src: "{{ build_workdir }}/{{ base_image_name }}.qcow2"
    dest: "{{ storage_pool_target }}/{{ base_image_name }}.qcow2"
    remote_src: true
    owner: root
    group: root
    mode: "0644"
  when: (not staged_volume.stat.exists) or (force_image_rebuild | bool)

- name: Refresh the storage pool so libvirt sees the new volume
  community.libvirt.virt_pool:
    name: default
    command: refresh

- name: Record the staged image sha256
  ansible.builtin.stat:
    path: "{{ storage_pool_target }}/{{ base_image_name }}.qcow2"
    checksum_algorithm: sha256
  register: staged_final

- name: Report the staged image identity
  ansible.builtin.debug:
    msg: "Staged {{ base_image_name }}.qcow2 sha256={{ staged_final.stat.checksum }}"
```

- [ ] **Step 3: Append the role to `image.yml`**

Edit `deploy/ansible/playbooks/image.yml` to run `guest_base_image` after `guest_image_prereqs`:

```yaml
---
- name: Build the operator-staged base image (slow; opt-in)
  hosts: remote_libvirt_hosts
  become: true
  gather_facts: true
  roles:
    - guest_image_prereqs
    - guest_base_image
```

- [ ] **Step 4: Syntax-check, lint, commit**

Run (from `deploy/ansible/`): `uv run --with 'ansible-core>=2.17' ansible-playbook playbooks/image.yml --syntax-check -i inventory/hosts.yml`
Expected: syntax OK.

Run: `just lint-ansible`
Expected: clean (resolve any `no-changed-when`/`command-instead-of-module` findings ansible-lint raises on the `command` tasks; the `changed_when` is set explicitly).

```bash
git add deploy/ansible/roles/guest_base_image deploy/ansible/playbooks/image.yml
git commit -m "feat(ansible): guest_base_image role (native build + cloud-image fallback)"
```

---

## Task 8: `pki.yml` playbook (controller-side CA + certs, client bundle born controller-side)

**Files:**
- Create: `deploy/ansible/playbooks/pki.yml`

**Interfaces:**
- Produces: `{{ pki_artifacts_dir }}/cacert.pem` (+ vaulted `cakey.pem`), per-host `{{ pki_artifacts_dir }}/<host>/{servercert.pem,serverkey.pem}` (consumed by `libvirt_tls`), and the worker client bundle `{{ pki_artifacts_dir }}/client/{clientcert.pem,clientkey.pem}` — all written controller-side, never fetched from a host.

- [ ] **Step 1: Create the PKI playbook**

Create `deploy/ansible/playbooks/pki.yml`:

```yaml
---
- name: Generate the fleet CA and per-host certs (controller-side)
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    pki_artifacts_dir: "{{ playbook_dir }}/../artifacts"
  tasks:
    - name: Ensure the artifacts directory exists
      ansible.builtin.file:
        path: "{{ item }}"
        state: directory
        mode: "0750"
      loop:
        - "{{ pki_artifacts_dir }}"
        - "{{ pki_artifacts_dir }}/client"

    # --- byo: install operator-supplied PEMs and skip generation ---
    - name: BYO mode — verify operator PEMs are present
      ansible.builtin.stat:
        path: "{{ pki_artifacts_dir }}/cacert.pem"
      register: byo_ca
      when: pki_mode == 'byo'

    - name: BYO mode — fail if the CA is missing
      ansible.builtin.assert:
        that: [byo_ca.stat.exists]
        fail_msg: "pki_mode=byo but {{ pki_artifacts_dir }}/cacert.pem is absent."
      when: pki_mode == 'byo'

    # --- generate mode (default) ---
    - name: Generate the CA private key
      community.crypto.openssl_privatekey:
        path: "{{ pki_artifacts_dir }}/cakey.pem"
        mode: "0600"
      when: pki_mode == 'generate'

    - name: Generate the CA CSR
      community.crypto.openssl_csr:
        path: "{{ pki_artifacts_dir }}/ca.csr"
        privatekey_path: "{{ pki_artifacts_dir }}/cakey.pem"
        common_name: kdive remote-libvirt CA
        basic_constraints: ["CA:TRUE"]
        basic_constraints_critical: true
        key_usage: ["keyCertSign", "cRLSign"]
        key_usage_critical: true
      when: pki_mode == 'generate'

    - name: Self-sign the CA certificate
      community.crypto.x509_certificate:
        path: "{{ pki_artifacts_dir }}/cacert.pem"
        privatekey_path: "{{ pki_artifacts_dir }}/cakey.pem"
        csr_path: "{{ pki_artifacts_dir }}/ca.csr"
        provider: selfsigned
        mode: "0644"
      when: pki_mode == 'generate'

    - name: Generate per-host server keys
      community.crypto.openssl_privatekey:
        path: "{{ pki_artifacts_dir }}/{{ item }}/serverkey.pem"
        mode: "0600"
      loop: "{{ groups['remote_libvirt_hosts'] }}"
      when: pki_mode == 'generate'

    - name: Generate per-host server CSRs (SAN = FQDN + IP)
      community.crypto.openssl_csr:
        path: "{{ pki_artifacts_dir }}/{{ item }}/server.csr"
        privatekey_path: "{{ pki_artifacts_dir }}/{{ item }}/serverkey.pem"
        common_name: "{{ hostvars[item].remote_host_fqdn }}"
        subject_alt_name:
          - "DNS:{{ hostvars[item].remote_host_fqdn }}"
          - "IP:{{ hostvars[item].gdb_addr }}"
        extended_key_usage: ["serverAuth"]
      loop: "{{ groups['remote_libvirt_hosts'] }}"
      when: pki_mode == 'generate'

    - name: Sign per-host server certs with the CA
      community.crypto.x509_certificate:
        path: "{{ pki_artifacts_dir }}/{{ item }}/servercert.pem"
        csr_path: "{{ pki_artifacts_dir }}/{{ item }}/server.csr"
        provider: ownca
        ownca_path: "{{ pki_artifacts_dir }}/cacert.pem"
        ownca_privatekey_path: "{{ pki_artifacts_dir }}/cakey.pem"
        mode: "0644"
      loop: "{{ groups['remote_libvirt_hosts'] }}"
      when: pki_mode == 'generate'

    # --- the worker client bundle: born controller-side, never fetched ---
    - name: Generate the worker client key
      community.crypto.openssl_privatekey:
        path: "{{ pki_artifacts_dir }}/client/clientkey.pem"
        mode: "0600"
      when: pki_mode == 'generate'

    - name: Generate the worker client CSR
      community.crypto.openssl_csr:
        path: "{{ pki_artifacts_dir }}/client/client.csr"
        privatekey_path: "{{ pki_artifacts_dir }}/client/clientkey.pem"
        common_name: kdive-worker
        extended_key_usage: ["clientAuth"]
      when: pki_mode == 'generate'

    - name: Sign the worker client cert with the CA
      community.crypto.x509_certificate:
        path: "{{ pki_artifacts_dir }}/client/clientcert.pem"
        csr_path: "{{ pki_artifacts_dir }}/client/client.csr"
        provider: ownca
        ownca_path: "{{ pki_artifacts_dir }}/cacert.pem"
        ownca_privatekey_path: "{{ pki_artifacts_dir }}/cakey.pem"
        mode: "0644"
      when: pki_mode == 'generate'

    - name: Copy the CA cert into the client bundle dir
      ansible.builtin.copy:
        src: "{{ pki_artifacts_dir }}/cacert.pem"
        dest: "{{ pki_artifacts_dir }}/client/cacert.pem"
        mode: "0644"
      when: pki_mode == 'generate'
```

> The per-host server key/CSR tasks write to `{{ pki_artifacts_dir }}/<host>/...`; `community.crypto` does not create parent dirs, so add a directory task before them:

```yaml
    - name: Ensure per-host PKI subdirectories exist
      ansible.builtin.file:
        path: "{{ pki_artifacts_dir }}/{{ item }}"
        state: directory
        mode: "0750"
      loop: "{{ groups['remote_libvirt_hosts'] }}"
      when: pki_mode == 'generate'
```

(Place this directly after the "Ensure the artifacts directory exists" task.)

- [ ] **Step 2: Syntax-check and lint**

Run (from `deploy/ansible/`): `uv run --with 'ansible-core>=2.17' --with community.crypto ansible-playbook playbooks/pki.yml --syntax-check -i inventory/hosts.yml`
Expected: syntax OK.

Run: `just lint-ansible`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add deploy/ansible/playbooks/pki.yml
git commit -m "feat(ansible): pki.yml — controller-side CA, per-host server certs, worker bundle"
```

---

## Task 9: `deploy/ansible/README.md` and final integration

**Files:**
- Create: `deploy/ansible/README.md`
- Verify: full `site.yml` ordering, all playbooks syntax-check, structural test, full lint gate.

**Interfaces:**
- Produces: the operator-facing README (mirroring `deploy/*/README.md`), and a final green gate proving the whole project lints and the structural test passes.

- [ ] **Step 1: Verify `site.yml` final play ordering**

Confirm `deploy/ansible/site.yml` runs the plays in this order (stack → tls → pool/net → acl → facts):

```yaml
---
- name: Install the virtualization stack (switch-to-modular)
  hosts: remote_libvirt_hosts
  become: true
  gather_facts: true
  roles: [libvirt_stack]

- name: Configure mutual-TLS listener
  hosts: remote_libvirt_hosts
  become: true
  gather_facts: true
  roles: [libvirt_tls]

- name: Define the storage pool and network
  hosts: remote_libvirt_hosts
  become: true
  gather_facts: true
  roles: [libvirt_pool_net]

- name: Apply the gdbstub-port ACL
  hosts: remote_libvirt_hosts
  become: true
  gather_facts: true
  roles: [gdbstub_acl]

- name: Emit remote-libvirt facts (controller-side)
  hosts: remote_libvirt_hosts
  gather_facts: true
  roles: [remote_libvirt_facts]
```

If the ordering differs, fix it now.

- [ ] **Step 2: Write the README**

Create `deploy/ansible/README.md`:

```markdown
# Ansible: remote-libvirt host bring-up

Automates `docs/operating/runbooks/remote-libvirt-host-setup.md` steps 1–6 for a
kdive **remote-libvirt** provider host (Ubuntu 26.04 / Fedora 44 / RHEL-Rocky 10,
x86_64 or ppc64le). Design: `docs/superpowers/specs/2026-06-18-ansible-remote-libvirt-host-setup-design.md`.

## What it produces

- A host running the **modular** libvirt daemons (all `libvirtd*` masked) with a
  `qemu+tls` mutual-TLS listener on `:16514` owned by `virtproxyd-tls.socket`.
- A `dir` storage pool + the `default` network.
- A firewalld/ufw ACL restricting `:16514` and the gdbstub range to `worker_cidr`.
- An operator-staged base image carrying the in-guest helpers (optional; `image.yml`).
- A controller-side `systems.toml` `[[remote_libvirt]]`/`[[image]]` block per host.

The default image is **lean** (provision / `host_dump` path). Set
`include_kernel_debuginfo: true` (and the crashkernel vars) for a drgn-live/kdump image.

## Layout

- `site.yml` — full host bring-up: stack → tls → pool/net → acl → emit facts.
- `playbooks/pki.yml` — controller-side CA + per-host server certs + worker client
  bundle. Run once. The client bundle is written to `artifacts/client/` (gitignored,
  **vault the keys**); it is never fetched back from a host.
- `playbooks/image.yml` — the slow base-image build (`virt-builder` or the
  `cloud-image` fallback for ppc64le). Opt-in.
- `inventory/` — `hosts.yml`, `group_vars/all.yml` (the tunable surface), per-host vars.

## Usage

```bash
cd deploy/ansible
ansible-galaxy collection install -r requirements.yml

# 1. Generate PKI (controller-side, once). Vault the private keys.
ansible-playbook playbooks/pki.yml

# 2. Bring up the host(s).
ansible-playbook site.yml

# 3. Build + stage the base image (slow; opt-in).
ansible-playbook playbooks/image.yml
```

## Config surface (`inventory/group_vars/all.yml`)

`disable_security_driver` defaults **false** (opt-in; try the SELinux/AppArmor label
fix first). `include_kernel_debuginfo` defaults **false**. `base_image_source` is
`virt-builder` (default) or `cloud-image`. `vcpus`/`memory_mb` are the required
billable ceiling. `gdb_addr` has no default — set it per host (provisioning fails
closed without it).

## Verification

- Idempotence: `ansible-playbook site.yml` twice → the second run reports **0 changed**.
- The off-CIDR ACL refusal (the firewalld assertion is the in-play proxy): from a host
  **outside** `worker_cidr`, a TCP connect to `:16514` must be refused/timed out, while
  the worker connects. Then `just check-remote-libvirt <host>` and the runbook step-8
  worker→host TLS connect confirm the path end-to-end.
- Only **one** host's `[[remote_libvirt]]` block may be loaded into a given
  `systems.toml` (the reconciler is singleton until multi-instance remote selection lands).

## Caveats

- ppc64le paths are implemented but **unvalidated** (no ppc64le test host).
- Molecule-in-Docker is intentionally not used: it cannot exercise KVM or `virt-builder`.
```

- [ ] **Step 3: Full gate**

Run: `just lint-ansible`
Expected: clean.

Run (from `deploy/ansible/`):
```bash
for p in site.yml playbooks/pki.yml playbooks/image.yml; do
  uv run --with 'ansible-core>=2.17' --with community.crypto \
    ansible-playbook "$p" --syntax-check -i inventory/hosts.yml
done
```
Expected: each prints `playbook: <name>` (syntax OK).

Run: `uv run python -m pytest tests/deploy/test_remote_libvirt_facts.py -q`
Expected: PASS.

Run the full suite (boundary/arch tests live outside `tests/deploy/`):
`just test`
Expected: PASS (no regressions from the new test module).

- [ ] **Step 4: Commit**

```bash
git add deploy/ansible/README.md deploy/ansible/site.yml
git commit -m "docs(ansible): operator README and final site.yml ordering"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| `libvirt_stack` (switch-to-modular, mask `libvirtd*`, KVM assert) | Task 2 |
| `libvirt_tls` (virtproxyd.conf, security driver opt-in, `:16514` + socket-ownership gate) | Task 3 |
| `libvirt_pool_net` | Task 4 |
| `guest_image_prereqs` (Ubuntu libguestfs fixes) | Task 6 |
| `guest_base_image` (native build + cloud-image fallback, helpers, debuginfo/crashkernel) | Task 7 |
| `gdbstub_acl` (rich rules + negative verification) | Task 5 |
| `remote_libvirt_facts` (exact `systems.toml` block, no fetch) | Task 1 |
| PKI play (bundle controller-side) | Task 8 |
| Config surface (`group_vars/all.yml`) | Task 1 |
| Distro/arch matrix (per-os_family + arch vars) | Tasks 2, 5, 7 |
| Idempotency & safety (`changed_when`, handlers, no fetch) | Tasks 2, 3, 5, 7 |
| Testing strategy (lint gate ×3, structural test, syntax-check) | Tasks 1, 9 |
| Deliverables (README, lint-ansible, tests/deploy) | Tasks 1, 9 |

**Type/name consistency checks performed:**
- `base_image_name` is defined once in `group_vars/all.yml` (Task 1) and consumed by the facts template (Task 1) and `guest_base_image` (Task 7) — same name.
- `pki_artifacts_dir` is defined in `group_vars/all.yml` (Task 1), re-declared as a play var in `pki.yml` (Task 8) to the same relative path, and consumed by `libvirt_tls` (Task 3) and `remote_libvirt_facts` (Task 1).
- The server-cert paths written by `pki.yml` (`<dir>/<host>/servercert.pem`, `serverkey.pem`, Task 8) exactly match the paths `libvirt_tls` reads (Task 3).
- `client_cert_ref`/`client_key_ref`/`ca_cert_ref` default filenames (`clientcert.pem`/`clientkey.pem`/`cacert.pem`, Task 1) match the worker bundle filenames the PKI play writes (Task 8) and the structural test's required field set (Task 1).
- `modular_sockets` (Task 2) deliberately omits `virtproxyd-tls.socket`; `libvirt_tls` (Task 3) enables it — matches the spec's role split.

**Known executor follow-ups (call out, do not silently skip):**
- Pin exact `==` versions for `ansible-lint`/`ansible-core`/collections when executing (look up current stable; do not assume).
- `ansible-lint` `profile: production` may flag the `command` tasks in `guest_base_image`; the `changed_when` is set, but resolve any residual `no-changed-when`/`command-instead-of-module` findings with explicit task-level `# noqa` only if a module genuinely cannot express the step (virt-builder/virt-customize have no module).
- The firewalld rich-rule precedence (source-accept beats priority-100 drop) must be confirmed on a real Fedora/RHEL host during acceptance; the in-play assertion only proves the drop rule exists.
```
