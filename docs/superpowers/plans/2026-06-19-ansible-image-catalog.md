# Ansible Image Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the ansible `guest_base_image` role into a selectable, expandable rootfs catalog (fedora/ubuntu/rocky/bare) with per-host image selection, replacing the single hard-coded `base_image_*` globals (#598).

**Architecture:** A `kdive_image_catalog` data list + `kdive_image_defaults` map in `group_vars`; per-host `host_images` selection resolved into `kdive_selected_images` (a derived group var both roles read); a `guest_base_image` role that loops over the selection dispatching per `source` (`virt-builder` / `cloud-image` / `scratch`); and a `remote_libvirt_facts` template emitting one `[[image]]` block per selected image plus the host's default `base_image`. No `src/kdive/**` change.

**Tech Stack:** Ansible (ansible-core 2.21.1, ansible-lint 26.4.0), Jinja2 templates, Python 3.14 + pytest for the contract test, `InventoryDoc` (`src/kdive/inventory/model.py`).

## Global Constraints

- **No `src/kdive/**` change.** App already models `image: list[ImageEntry]`; the gap is ansible-only.
- **Replace, don't deprecate:** remove the `base_image_distro`/`_version`/`_name`/`_source` globals entirely; nothing reads them after this change.
- **Helper/package contract is Fedora/RHEL-family.** Helpers use `grubby`/`dracut`/`grub2-reboot`/`kdump-utils`. Full kdive arc ships for fedora+rocky; ubuntu stages/boots/agent-only (install arc tracked separately). The ubuntu entry carries its own Debian `packages`.
- **`root_device` is remote metadata** (default `/dev/vda`); the remote provider never consumes it (ADR-0183). Do not invent partition numbers.
- **The `systems_toml_block.j2` image loop stays free of ansible-only filters** (`| bool`, etc.) so the contract test renders it under plain Jinja2.
- **Guardrail before every commit:** `just lint-ansible` (yamllint + ansible-lint + `ansible-playbook --syntax-check`). For Task 1 also `uv run python -m pytest tests/inventory/test_image_catalog_contract.py -q`.
- Conventional commits, ≤72-char subject, `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.
- ADR: [0188](../../adr/0188-ansible-image-catalog.md). Spec: `docs/superpowers/specs/2026-06-19-ansible-image-catalog-design.md`.

---

## File Structure

- `deploy/ansible/inventory/group_vars/all.yml` — replace `base_image_*` with `kdive_image_defaults`, `kdive_image_catalog`, `host_images_default`, and the derived selection vars.
- `deploy/ansible/inventory/host_vars/{ub26-big,fed44-big,rock10-big}.yml` — add `host_images` (+ optional `host_default_image`).
- `deploy/ansible/roles/guest_base_image/defaults/main.yml` — drop the single-image cloud-url default; keep workdir.
- `deploy/ansible/roles/guest_base_image/tasks/main.yml` — selection validation + loop `include_tasks: build_one.yml`.
- `deploy/ansible/roles/guest_base_image/tasks/build_one.yml` — per-image build (old main.yml body, parameterized by `image`).
- `deploy/ansible/roles/guest_base_image/tasks/build_scratch.yml` — bare image, from host OS family.
- `deploy/ansible/roles/guest_image_prereqs/tasks/main.yml` — ensure scratch build tools present.
- `deploy/ansible/roles/remote_libvirt_facts/tasks/main.yml` — no logic change needed (template reads group vars).
- `deploy/ansible/roles/remote_libvirt_facts/templates/systems_toml_block.j2` — N `[[image]]` blocks + default `base_image`.
- `deploy/ansible/README.md` — catalog config surface + ubuntu/scratch caveats.
- `tests/inventory/test_image_catalog_contract.py` — render the real template + `InventoryDoc.parse`.

---

## Task 1: Data model + facts template + contract test (the CI-gated core)

**Files:**
- Modify: `deploy/ansible/inventory/group_vars/all.yml`
- Modify: `deploy/ansible/inventory/host_vars/ub26-big.yml`, `fed44-big.yml`, `rock10-big.yml`
- Modify: `deploy/ansible/roles/remote_libvirt_facts/templates/systems_toml_block.j2`
- Create: `tests/inventory/test_image_catalog_contract.py`

**Interfaces:**
- Produces (group vars, read by Task 2 and the facts template):
  - `kdive_image_defaults` (dict: `packages`, `helpers`, `include_kernel_debuginfo`, `crashkernel`, `arches`, `root_device`, `arch_alias`)
  - `kdive_image_catalog` (list of entry dicts: `name`, `distro`, `version`, `source`, optional `cloud_image_url`/`packages`/`arches`/`root_device`/`force`)
  - `kdive_requested_images = host_images | default(host_images_default)`
  - `kdive_selected_images = kdive_image_catalog | selectattr('name','in',kdive_requested_images) | list`
  - `kdive_default_image = host_default_image | default(kdive_requested_images[0])`
  - `image_arch_alias = kdive_image_defaults.arch_alias.get(ansible_architecture, ansible_architecture)`
- Template consumes: `kdive_selected_images`, `kdive_default_image`, `kdive_image_defaults`, `ansible_architecture`, plus the existing `[[remote_libvirt]]` vars.

- [ ] **Step 1: Write the failing contract test**

Create `tests/inventory/test_image_catalog_contract.py`:

```python
"""Contract test: the ansible facts template emits a systems.toml the app accepts.

Renders the real ``systems_toml_block.j2`` (the ansible -> app seam) with a
representative four-image host context and asserts ``InventoryDoc.parse`` accepts
it: image identities unique, ``base_image`` resolves, every source is ``staged``.
A template typo (wrong field, missing ``[image.source]``) makes this fail.
"""

from __future__ import annotations

from pathlib import Path

import jinja2

from kdive.inventory.model import InventoryDoc

_TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "deploy/ansible/roles/remote_libvirt_facts/templates/systems_toml_block.j2"
)

_DEFAULTS = {
    "packages": ["qemu-guest-agent"],
    "helpers": ["kdive-install-kernel"],
    "include_kernel_debuginfo": False,
    "crashkernel": "256M",
    "arches": ["x86_64"],
    "root_device": "/dev/vda",
    "arch_alias": {"x86_64": "amd64", "aarch64": "arm64", "ppc64le": "ppc64el"},
}

# A four-image selection (fedora/ubuntu/rocky/bare) as the role would resolve it.
_SELECTED = [
    {"name": "fedora-kdive-remote-base-43", "distro": "fedora", "source": "virt-builder"},
    {"name": "ubuntu-2404-kdive-remote-base", "distro": "ubuntu", "source": "cloud-image"},
    {"name": "rocky-10-kdive-remote-base", "distro": "rocky", "source": "cloud-image"},
    {"name": "bare-kdive-remote-base", "distro": "bare", "source": "scratch",
     "root_device": "/dev/vda1"},
]

_CONTEXT = {
    "kdive_image_defaults": _DEFAULTS,
    "kdive_selected_images": _SELECTED,
    "kdive_default_image": "fedora-kdive-remote-base-43",
    "ansible_architecture": "x86_64",
    "inventory_hostname": "host-a",
    "remote_host_fqdn": "host-a.example.test",
    "gdb_addr": "192.168.12.2",
    "gdbstub_range": "47000:47099",
    "remote_libvirt_facts_client_cert_ref": "clientcert.pem",
    "remote_libvirt_facts_client_key_ref": "clientkey.pem",  # pragma: allowlist secret
    "remote_libvirt_facts_ca_cert_ref": "cacert.pem",
    "cost_class": "remote",
    "concurrent_allocation_cap": 1,
    "vcpus": 16,
    "memory_mb": 65536,
    "shapes": ["small", "medium", "large", "max"],
    "machine_type": {"x86_64": "pc", "ppc64le": "pseries"},
}


def _render() -> str:
    text = _TEMPLATE.read_text(encoding="utf-8")
    return jinja2.Template(text, undefined=jinja2.StrictUndefined).render(**_CONTEXT)


def test_template_emits_one_image_block_per_selected_image() -> None:
    doc = InventoryDoc.parse(_render())
    names = sorted(img.name for img in doc.image)
    assert names == sorted(i["name"] for i in _SELECTED)


def test_template_image_identities_unique_and_staged() -> None:
    doc = InventoryDoc.parse(_render())
    for img in doc.image:
        assert img.provider == "remote-libvirt"
        assert img.arch == "x86_64"
        assert img.source.kind == "staged"
        assert img.source.volume == f"{img.name}.qcow2"


def test_template_default_base_image_resolves() -> None:
    doc = InventoryDoc.parse(_render())
    declared = {img.name for img in doc.image}
    assert doc.remote_libvirt[0].base_image in declared
    assert doc.remote_libvirt[0].base_image == "fedora-kdive-remote-base-43"
```

- [ ] **Step 2: Confirm `InventoryDoc.parse` signature**

Run: `uv run python -c "from kdive.inventory.model import InventoryDoc; import inspect; print(inspect.signature(InventoryDoc.parse))"`
If `parse` takes a TOML string, the test stands. If it takes a path/bytes, adapt `_render()`/the call (e.g. `tomllib.loads` then `InventoryDoc(**data)`), keeping the three assertions. Verify `.image[*].source.volume`, `.remote_libvirt[*].base_image` attribute names against `model.py`.

- [ ] **Step 3: Run the test, verify it fails**

Run: `uv run python -m pytest tests/inventory/test_image_catalog_contract.py -q`
Expected: FAIL — the current template renders a single hard-coded `[[image]]` (no loop, references `base_image_name`), so either Jinja2 `StrictUndefined` raises on `base_image_name` or the parsed doc has one image whose name != the selection. (A `StrictUndefined` error is an acceptable red.)

- [ ] **Step 4: Rewrite the facts template's image section as a loop**

Replace the single `[[image]]` block at the top of `systems_toml_block.j2` with a loop, and set `base_image` to `kdive_default_image`. Final template:

```jinja
# Rendered by the remote_libvirt_facts role for {{ inventory_hostname }}.
# Paste ONE host's [[remote_libvirt]] block into the deployment's systems.toml
# (the reconciler rejects multiple [[remote_libvirt]] blocks — singleton until
# multi-instance remote selection lands). The [[image]] blocks below are safe to
# include alongside it. Machine type is an env knob, not a systems.toml field:
#   KDIVE_REMOTE_LIBVIRT_MACHINE={{ machine_type[ansible_architecture] }}

{% for img in kdive_selected_images %}
[[image]]
provider = "remote-libvirt"
name = "{{ img.name }}"
arch = "{{ ansible_architecture }}"
format = "qcow2"
root_device = "{{ img.root_device | default(kdive_image_defaults.root_device) }}"
visibility = "public"
[image.source]
kind = "staged"
volume = "{{ img.name }}.qcow2"

{% endfor %}
[[remote_libvirt]]
name = "{{ inventory_hostname }}"
uri = "qemu+tls://{{ remote_host_fqdn }}/system"
gdb_addr = "{{ gdb_addr }}"
gdbstub_range = "{{ gdbstub_range }}"
client_cert_ref = "{{ remote_libvirt_facts_client_cert_ref }}"
client_key_ref = "{{ remote_libvirt_facts_client_key_ref }}"   # pragma: allowlist secret - filename ref
ca_cert_ref = "{{ remote_libvirt_facts_ca_cert_ref }}"
base_image = "{{ kdive_default_image }}"
cost_class = "{{ cost_class }}"
concurrent_allocation_cap = {{ concurrent_allocation_cap }}
vcpus = {{ vcpus }}
memory_mb = {{ memory_mb }}
shapes = [{% for s in shapes %}"{{ s }}"{% if not loop.last %}, {% endif %}{% endfor %}]
```

- [ ] **Step 5: Run the test, verify it passes**

Run: `uv run python -m pytest tests/inventory/test_image_catalog_contract.py -q`
Expected: PASS (3 tests).

- [ ] **Step 6: Replace the `base_image_*` globals with the catalog in `group_vars/all.yml`**

Remove the `# --- base image ---` block (`base_image_distro`/`_version`/`_name`/`_source`, `build_packages`, `include_kernel_debuginfo`, `crashkernel_value`). Keep `helper_src` and `force_image_rebuild`. Add:

```yaml
# --- image catalog (replaces the single base_image_* globals) ---
helper_src: "{{ playbook_dir }}/../remote-libvirt-guest-helpers"
force_image_rebuild: false

kdive_image_defaults:
  # Fedora/RHEL-family package set; the ubuntu entry overrides it.
  packages:
    - qemu-guest-agent
    - drgn
    - kexec-tools
    - makedumpfile
    - kdump-utils
    - curl
    - tar
    - openssl
    - python3
  helpers:
    - kdive-install-kernel
    - kdive-capture-vmcore
    - kdive-drgn
  include_kernel_debuginfo: false
  crashkernel: "256M"
  arches: [x86_64]
  root_device: /dev/vda
  arch_alias:
    x86_64: amd64
    aarch64: arm64
    ppc64le: ppc64el

kdive_image_catalog:
  - name: fedora-kdive-remote-base-43
    distro: fedora
    version: "43"
    source: virt-builder
  - name: ubuntu-2404-kdive-remote-base
    distro: ubuntu
    version: "24.04"
    source: cloud-image
    cloud_image_url: "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-{{ image_arch_alias }}.img"
    packages:
      - qemu-guest-agent
      - kexec-tools
      - makedumpfile
      - kdump-tools
      - curl
      - tar
      - openssl
      - python3
  - name: rocky-10-kdive-remote-base
    distro: rocky
    version: "10"
    source: cloud-image
    cloud_image_url: "https://dl.rockylinux.org/pub/rocky/10/images/{{ ansible_architecture }}/Rocky-10-GenericCloud-Base.latest.{{ ansible_architecture }}.qcow2"
  - name: bare-kdive-remote-base
    distro: bare
    version: "1"
    source: scratch

host_images_default: [fedora-kdive-remote-base-43]

# Derived selection (lazily evaluated; read by guest_base_image + remote_libvirt_facts).
kdive_requested_images: "{{ host_images | default(host_images_default) }}"
kdive_selected_images: "{{ kdive_image_catalog | selectattr('name', 'in', kdive_requested_images) | list }}"
kdive_default_image: "{{ host_default_image | default(kdive_requested_images[0]) }}"
image_arch_alias: "{{ kdive_image_defaults.arch_alias.get(ansible_architecture | default('x86_64'), ansible_architecture | default('x86_64')) }}"
```

- [ ] **Step 7: Add `host_images` to each host_vars file**

`ub26-big.yml` (Ubuntu host — stage ubuntu + bare; default stays fedora so the registered `base_image` is a full-arc image... but ub26 cannot build fedora natively without a virt-builder fedora template; choose a host-appropriate default). Set the ub26 host to stage ubuntu + bare and default to ubuntu:

```yaml
host_images: [ubuntu-2404-kdive-remote-base, bare-kdive-remote-base]
host_default_image: ubuntu-2404-kdive-remote-base
```

`fed44-big.yml` (Fedora host — full arc):

```yaml
host_images: [fedora-kdive-remote-base-43, bare-kdive-remote-base]
host_default_image: fedora-kdive-remote-base-43
```

`rock10-big.yml` (Rocky host — RHEL-family, full arc):

```yaml
host_images: [rocky-10-kdive-remote-base, bare-kdive-remote-base]
host_default_image: rocky-10-kdive-remote-base
```

(Keep each file's existing `remote_host_fqdn` / `gdb_addr`.)

- [ ] **Step 8: Run lint + contract test, then commit**

Run: `just lint-ansible && uv run python -m pytest tests/inventory/test_image_catalog_contract.py -q`
Expected: both green.

```bash
git add deploy/ansible/inventory tests/inventory/test_image_catalog_contract.py \
        deploy/ansible/roles/remote_libvirt_facts/templates/systems_toml_block.j2
git commit -m "feat(ansible): emit a per-host image catalog in systems.toml facts"
```

---

## Task 2: `guest_base_image` role — selection loop + per-image build

**Files:**
- Modify: `deploy/ansible/roles/guest_base_image/tasks/main.yml`
- Create: `deploy/ansible/roles/guest_base_image/tasks/build_one.yml`
- Modify: `deploy/ansible/roles/guest_base_image/defaults/main.yml`

**Interfaces:**
- Consumes: `kdive_selected_images`, `kdive_requested_images`, `kdive_image_catalog`, `kdive_image_defaults`, `image_arch_alias`, `helper_src`, `storage_pool_target`, `force_image_rebuild` (from Task 1 / existing group vars).
- `build_one.yml` is `include_tasks`-d with `loop_var: image`; inside, the per-image effective fields are `image.<field> | default(kdive_image_defaults.<field>)`.

- [ ] **Step 1: Rewrite `tasks/main.yml` as validate-then-loop**

```yaml
---
- name: Assert every selected image name exists in the catalog
  ansible.builtin.assert:
    that: item in (kdive_image_catalog | map(attribute='name') | list)
    fail_msg: >-
      host_images entry '{{ item }}' is not defined in kdive_image_catalog
      (group_vars/all.yml).
  loop: "{{ kdive_requested_images }}"

- name: Assert each selected image supports this host architecture
  ansible.builtin.assert:
    that: ansible_architecture in (item.arches | default(kdive_image_defaults.arches))
    fail_msg: >-
      image '{{ item.name }}' does not support arch {{ ansible_architecture }}
      (arches={{ item.arches | default(kdive_image_defaults.arches) }}).
  loop: "{{ kdive_selected_images }}"
  loop_control:
    label: "{{ item.name }}"

- name: Ensure the build working directory exists
  ansible.builtin.file:
    path: "{{ guest_base_image_build_workdir }}"
    state: directory
    mode: "0755"

- name: Build and stage each selected image
  ansible.builtin.include_tasks: build_one.yml
  loop: "{{ kdive_selected_images }}"
  loop_control:
    loop_var: image
    label: "{{ image.name }}"
```

- [ ] **Step 2: Create `tasks/build_one.yml` (per-image, parameterized by `image`)**

Port the old single-image body, replacing globals with `image` fields. Compute per-image facts first so the rest reads cleanly:

```yaml
---
- name: "[{{ image.name }}] Resolve effective image fields"
  ansible.builtin.set_fact:
    img_packages: >-
      {{ (image.packages | default(kdive_image_defaults.packages))
         + (['kernel-debuginfo']
            if (image.include_kernel_debuginfo | default(kdive_image_defaults.include_kernel_debuginfo)) | bool
            else []) }}
    img_helpers: "{{ image.helpers | default(kdive_image_defaults.helpers) }}"
    img_crashkernel: "{{ image.crashkernel | default(kdive_image_defaults.crashkernel) }}"
    img_debuginfo: "{{ image.include_kernel_debuginfo | default(kdive_image_defaults.include_kernel_debuginfo) }}"
    img_force: "{{ (force_image_rebuild | bool) or (image.force | default(false) | bool) }}"
    img_qcow2: "{{ guest_base_image_build_workdir }}/{{ image.name }}.qcow2"

- name: "[{{ image.name }}] Check whether the staged volume already exists"
  ansible.builtin.stat:
    path: "{{ storage_pool_target }}/{{ image.name }}.qcow2"
  register: img_staged

- name: "[{{ image.name }}] Build (virt-builder, native)"
  ansible.builtin.command:  # noqa: command-instead-of-module
    argv:
      - virt-builder
      - "{{ image.distro }}-{{ image.version }}"
      - --arch
      - "{{ ansible_architecture }}"
      - --format
      - qcow2
      - --size
      - 10G
      - --output
      - "{{ img_qcow2 }}"
      - --install
      - "{{ img_packages | join(',') }}"
      - --run-command
      - systemctl enable qemu-guest-agent.service
      - --run-command
      - systemctl enable kdump.service
      - --run-command
      - sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config
  when:
    - image.source == 'virt-builder'
    - (not img_staged.stat.exists) or img_force | bool
  changed_when: true

- name: "[{{ image.name }}] Download the cloud image (fallback path)"
  ansible.builtin.get_url:
    url: "{{ image.cloud_image_url }}"
    dest: "{{ img_qcow2 }}"
    mode: "0644"
  when:
    - image.source == 'cloud-image'
    - (not img_staged.stat.exists) or img_force | bool

- name: "[{{ image.name }}] Customize the cloud image"
  ansible.builtin.command:  # noqa: command-instead-of-module
    argv:
      - virt-customize
      - -a
      - "{{ img_qcow2 }}"
      - --install
      - "{{ img_packages | join(',') }}"
      - --run-command
      - systemctl enable qemu-guest-agent.service
      - --run-command
      - systemctl enable kdump.service
  when:
    - image.source == 'cloud-image'
    - (not img_staged.stat.exists) or img_force | bool
  changed_when: true

- name: "[{{ image.name }}] Build from scratch (bare, host OS family)"
  ansible.builtin.include_tasks: build_scratch.yml
  when:
    - image.source == 'scratch'
    - (not img_staged.stat.exists) or img_force | bool

- name: "[{{ image.name }}] Set the crashkernel reservation"
  ansible.builtin.command:  # noqa: command-instead-of-module
    argv:
      - virt-customize
      - -a
      - "{{ img_qcow2 }}"
      - --run-command
      - "grubby --update-kernel=ALL --args=crashkernel={{ img_crashkernel }}"
  when:
    - img_debuginfo | bool
    - (not img_staged.stat.exists) or img_force | bool
  changed_when: true

- name: "[{{ image.name }}] Build the virt-customize helper arguments"
  ansible.builtin.set_fact:
    img_helper_args: >-
      {{ img_helper_args | default([])
         + ['--copy-in', helper_src ~ '/' ~ item ~ ':/usr/local/sbin/',
            '--run-command', 'chown root:root /usr/local/sbin/' ~ item,
            '--run-command', 'chmod 0755 /usr/local/sbin/' ~ item,
            '--run-command', 'restorecon -v /usr/local/sbin/' ~ item] }}
  loop: "{{ img_helpers }}"

- name: "[{{ image.name }}] Install the in-guest helpers into the image"
  ansible.builtin.command:  # noqa: command-instead-of-module
    argv: >-
      {{ ['virt-customize', '-a', img_qcow2] + img_helper_args }}
  when: (not img_staged.stat.exists) or img_force | bool
  changed_when: true

- name: "[{{ image.name }}] Reset the per-image helper-args accumulator"
  ansible.builtin.set_fact:
    img_helper_args: []

- name: "[{{ image.name }}] Stage the finished image into the pool"
  ansible.builtin.copy:
    src: "{{ img_qcow2 }}"
    dest: "{{ storage_pool_target }}/{{ image.name }}.qcow2"
    remote_src: true
    owner: root
    group: root
    mode: "0644"
  when: (not img_staged.stat.exists) or img_force | bool

- name: "[{{ image.name }}] Refresh the storage pool"
  community.libvirt.virt_pool:
    name: default
    command: refresh

- name: "[{{ image.name }}] Record the staged image sha256"
  ansible.builtin.stat:
    path: "{{ storage_pool_target }}/{{ image.name }}.qcow2"
    checksum_algorithm: sha256
  register: img_final

- name: "[{{ image.name }}] Report the staged image identity"
  ansible.builtin.debug:
    msg: "Staged {{ image.name }}.qcow2 sha256={{ img_final.stat.checksum }}"
```

Note the `img_helper_args` reset at the end: `include_tasks` shares facts across loop iterations, so the accumulator MUST be cleared per image or helper args leak between images.

- [ ] **Step 3: Trim `defaults/main.yml`**

Remove `guest_base_image_helpers` and `guest_base_image_cloud_image_url` (now per-entry/in `kdive_image_defaults`). Keep `guest_base_image_build_workdir`:

```yaml
---
guest_base_image_build_workdir: "{{ ansible_env.HOME }}/kdive-image-build"
```

- [ ] **Step 4: Lint + syntax-check, then commit**

Run: `just lint-ansible`
Expected: green (yamllint + ansible-lint + `--syntax-check` on `playbooks/image.yml`).

```bash
git add deploy/ansible/roles/guest_base_image
git commit -m "feat(ansible): loop guest_base_image over the selected catalog"
```

---

## Task 3: Scratch (bare) build path + prereqs

**Files:**
- Create: `deploy/ansible/roles/guest_base_image/tasks/build_scratch.yml`
- Modify: `deploy/ansible/roles/guest_image_prereqs/tasks/main.yml`

**Interfaces:**
- Consumes: `image` (loop var), `img_qcow2`, `img_packages`, `ansible_os_family`, `guest_base_image_build_workdir`.
- Produces: a bootable qcow2 at `img_qcow2` with systemd + busybox + qemu-guest-agent + curl/tar + the (family) helpers installed by `build_one.yml`'s helper step afterward.

- [ ] **Step 1: Ensure scratch build tools in `guest_image_prereqs`**

Append to `guest_image_prereqs/tasks/main.yml` (outside the Debian-only block):

```yaml
- name: Ensure debootstrap is present (Debian-family scratch builds)
  ansible.builtin.apt:
    name: debootstrap
    state: present
  when: ansible_os_family == 'Debian'

- name: Ensure dnf + guestfs tools are present (RedHat-family scratch builds)
  ansible.builtin.dnf:
    name: [dnf, libguestfs-tools]
    state: present
  when: ansible_os_family == 'RedHat'
```

- [ ] **Step 2: Create `tasks/build_scratch.yml`**

Build a minimal root tree from the host family, then assemble a bootable qcow2 via guestfish. `root_device` for the bare entry stays metadata; the in-guest grub owns the real root.

```yaml
---
- name: "[{{ image.name }}] Scratch rootfs dir"
  ansible.builtin.file:
    path: "{{ guest_base_image_build_workdir }}/{{ image.name }}.root"
    state: directory
    mode: "0755"

- name: "[{{ image.name }}] Bootstrap a minimal RedHat-family rootfs"
  ansible.builtin.command:  # noqa: command-instead-of-module
    argv:
      - dnf
      - --installroot={{ guest_base_image_build_workdir }}/{{ image.name }}.root
      - --releasever={{ ansible_distribution_major_version }}
      - --setopt=install_weak_deps=False
      - -y
      - install
      - systemd
      - busybox
      - qemu-guest-agent
      - kexec-tools
      - makedumpfile
      - curl
      - tar
      - kernel
      - grub2-tools
      - grub2-efi-x64
      - shim-x64
  when: ansible_os_family == 'RedHat'
  changed_when: true

- name: "[{{ image.name }}] Bootstrap a minimal Debian-family rootfs"
  ansible.builtin.command:  # noqa: command-instead-of-module
    argv:
      - debootstrap
      - --variant=minbase
      - --include=systemd-sysv,busybox,qemu-guest-agent,kexec-tools,makedumpfile,curl,tar,linux-image-generic,grub-efi-amd64
      - "{{ ansible_distribution_release }}"
      - "{{ guest_base_image_build_workdir }}/{{ image.name }}.root"
  when: ansible_os_family == 'Debian'
  changed_when: true

- name: "[{{ image.name }}] Assemble the bootable qcow2 from the rootfs"
  ansible.builtin.command:  # noqa: command-instead-of-module
    # virt-make-fs builds a partitioned filesystem image; the bare image is then
    # bootable via the in-guest grub installed in the bootstrap above. UNVALIDATED
    # (no scratch-capable host in CI/available hardware — see README caveat).
    argv:
      - virt-make-fs
      - --partition=gpt
      - --type=ext4
      - --format=qcow2
      - --size=+2G
      - "{{ guest_base_image_build_workdir }}/{{ image.name }}.root"
      - "{{ img_qcow2 }}"
  changed_when: true

- name: "[{{ image.name }}] Enable qemu-guest-agent in the scratch image"
  ansible.builtin.command:  # noqa: command-instead-of-module
    argv:
      - virt-customize
      - -a
      - "{{ img_qcow2 }}"
      - --run-command
      - systemctl enable qemu-guest-agent.service
  changed_when: true
```

- [ ] **Step 3: Lint + syntax-check, then commit**

Run: `just lint-ansible`
Expected: green. (`ansible-lint` may warn on `command` use — the existing tasks carry `# noqa: command-instead-of-module`; keep that pattern.)

```bash
git add deploy/ansible/roles/guest_base_image/tasks/build_scratch.yml \
        deploy/ansible/roles/guest_image_prereqs/tasks/main.yml
git commit -m "feat(ansible): add the scratch (bare) build path from host OS family"
```

---

## Task 4: README documentation

**Files:**
- Modify: `deploy/ansible/README.md`

- [ ] **Step 1: Document the catalog config surface and caveats**

Replace the "Config surface" paragraph's base-image sentences with a catalog description: `kdive_image_catalog` + `kdive_image_defaults` in `group_vars`, per-host `host_images` + `host_default_image`. Under "Caveats", add:
- The in-guest helpers are Fedora/RHEL-family (`grubby`/`dracut`); the full kdive build→install→boot→debug arc ships for fedora + rocky. The **ubuntu** image stages, boots, and connects its guest agent (provision/`host_dump`), but `runs.install` needs a Debian helper variant — **unvalidated/out of scope**.
- The **scratch/bare** path is implemented but **unvalidated** (no scratch-capable test host), like the ppc64le note.

Update the "Usage" step 3 comment to note image build stages each host's `host_images`.

- [ ] **Step 2: Doc guardrails + commit**

Run: `just docs-links && just docs-paths`
Expected: green.

```bash
git add deploy/ansible/README.md
git commit -m "docs(ansible): document the image catalog + ubuntu/scratch caveats"
```

---

## Self-Review notes

- **Spec coverage:** catalog data (Task 1 step 6) ✓; per-host selection (Task 1 step 7) ✓; selection validation (Task 2 step 1) ✓; per-source build loop incl. scratch (Tasks 2-3) ✓; N `[[image]]` facts + default base_image (Task 1 step 4) ✓; per-image idempotency (`img_staged`/`img_force`, Task 2 step 2) ✓; contract test (Task 1) ✓; README caveats (Task 4) ✓.
- **Cross-task type consistency:** `kdive_selected_images`, `kdive_requested_images`, `kdive_default_image`, `image_arch_alias`, `img_qcow2`, `img_packages`, `img_helpers` names match across Tasks 1-3.
- **Known limitations (documented, not bugs):** scratch grub/boot assembly + ubuntu install arc + ppc64le are hardware-only and unvalidated; the contract test is the only CI behavioral guard for the seam.
