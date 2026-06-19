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
