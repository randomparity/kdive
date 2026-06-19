# Design: Ansible roles for remote-libvirt host bring-up

**Status:** Approved (brainstorm) — pending implementation plan
**Date:** 2026-06-18
**Worktree/branch:** `feat/ansible-remote-libvirt`
**Ground truth:** `docs/operating/runbooks/remote-libvirt-host-setup.md` (steps 1–6)

## Problem

The remote-libvirt provider needs a separate TLS target host: virtualization stack,
`qemu+tls` mutual TLS, storage pool/network, a firewall ACL, and an operator-staged base
image carrying the in-guest helpers. Today this is a manual runbook exercised only on
**Ubuntu 24.04 / x86_64**. We want repeatable, idempotent **Ansible** automation that also
covers **Ubuntu 26.04**, **Fedora 44**, **RHEL/Rocky 10**, and the **x86_64** + **ppc64le**
architectures.

## Scope

**In scope** (runbook steps 1–6, host-side): install stack, mutual-TLS PKI + `virtproxyd`
TLS listener, storage pool + network, libguestfs prerequisites, base-image build + in-guest
helpers, gdbstub/16514 firewall ACL. The automation also **emits** the `systems.toml`
`[[remote_libvirt]]`/`[[image]]` block and the worker client-cert bundle as controller-side
artifacts.

**Out of scope** (left to existing tooling; the roles emit their inputs): creating the
Kubernetes TLS Secret, applying the `systems.toml` ConfigMap into a running cluster, project
onboarding/quota/budget, and ephemeral build-host (`runs.build`) registration.

## Decisions (locked during brainstorm)

| Axis | Decision | Rationale |
|---|---|---|
| Boundary | Host config **+** base image (steps 1–6); emit `systems.toml` block | Produces a host fully ready to register without coupling to a live cluster |
| Daemon model | **Modular only** (`virtqemud`/`virtnetworkd`/`virtstoraged` + `virtproxyd`); mask `libvirtd` | Default on Fedora 44 / RHEL 10 and on existing modular hosts; monolithic `libvirtd` is deprecated. TLS socket is `virtproxyd-tls.socket` |
| Architecture | Parameterize all paths via `ansible_architecture`; **validate x86_64 now**, ppc64le designed-in but unvalidated | Both named test hosts are x86_64; a KVM host serves only its own-arch guests, so arch is a per-host property and the image is always native-built |
| PKI | **Shared fleet CA on the controller** (`community.crypto`); per-host server cert; one worker client cert; `pki_mode: generate\|byo` | With `auth_tls="none"` the CA is the authz boundary; one shared CA means the worker presents one client identity all hosts trust |
| Firewall | **Native per-distro**: `ansible.posix.firewalld` (Fedora/RHEL) + `community.general.ufw` (Ubuntu) | Each host's default manager; idempotent, persistent, no conflict with an existing ACL |
| Guest image distro | **Fedora-pinned** regardless of host distro/arch | The helpers use `grubby`/`dracut`/`dnf`/SELinux; Fedora is the tested helper contract |

## Approach

Composable **roles, one per runbook step**, orchestrated by a thin `site.yml`, plus a
controller-side PKI playbook and an opt-in image-build playbook.

Alternatives rejected: a monolithic playbook (can't run/test a single step or isolate the
slow image build) and a single many-task-file role (loses per-step variable scoping/reuse).

## Layout

```
deploy/ansible/
  README.md  ansible.cfg  requirements.yml      # community.crypto, community.general,
  inventory/                                    #   ansible.posix, community.libvirt
    hosts.yml                                   # ub26-big, fed44-big; grouped by os_family + arch
    group_vars/all.yml                          # the tunable surface
    host_vars/{ub26-big,fed44-big}.yml
  site.yml                                       # full bring-up: stack → tls → pool/net → acl → emit
  playbooks/
    pki.yml                                      # controller-side CA + per-host certs (run once)
    image.yml                                    # base-image build only (slow; opt-in)
  roles/
    libvirt_stack/        # step 1
    libvirt_tls/          # step 2
    libvirt_pool_net/     # step 3
    guest_image_prereqs/  # step 4  (Ubuntu-gated)
    guest_base_image/     # step 5 + 5a
    gdbstub_acl/          # step 6
    remote_libvirt_facts/ # emit systems.toml block + client bundle
```

`site.yml` runs roles 1, 2, 3, 6, then `remote_libvirt_facts`. The base image (roles 4–5) is
invoked via `playbooks/image.yml` (or a `build_image` tag) because `virt-builder` is slow and
should not run on every converge.

## Roles

Each role has one purpose; all distro/arch divergence is pushed into variables.

1. **`libvirt_stack`** — install virt packages (per-`os_family` lists; qemu chosen by arch:
   `qemu-system-x86` vs `qemu-system-ppc`), enable the modular daemon sockets, **mask
   `libvirtd`**, add the login user to `kvm`/`libvirt`, assert `virt-host-validate qemu`
   passes the KVM + `/dev/kvm` checks (fail fast otherwise).

2. **`libvirt_tls`** — install the server cert/key + CA cert (from the PKI step) into
   `/etc/pki/{CA,libvirt}`; write `/etc/libvirt/virtproxyd.conf` (`listen_tls=1`,
   `listen_tcp=0`, `auth_tls="none"`); enable `virtproxyd-tls.socket`; set
   `security_driver="none"` in `qemu.conf` (guarded by `disable_security_driver`, default
   `true`, with the overlay-backing-file permission rationale in a comment); restart via
   handler; assert `:16514` is `LISTEN`.

3. **`libvirt_pool_net`** — define/build/start/autostart the storage pool (`dir`, target a
   var) and the `default` network via `community.libvirt` modules (idempotent).

4. **`guest_image_prereqs`** — the **Ubuntu-only** libguestfs appliance fixes (kernel `0644`,
   `apparmor_restrict_unprivileged_userns=0`, unload `usr.bin.passt`, install
   `isc-dhcp-client`, clear the supermin cache), all `when: ansible_os_family == 'Debian'`.
   No-op on Fedora/RHEL. Image path only.

5. **`guest_base_image`** — build the Fedora base image with `virt-builder` (arch via
   `--arch`, version a var), install the package set, set SELinux permissive, **`--copy-in`
   the three helpers from `deploy/remote-libvirt-guest-helpers/` with `chown root:root` +
   `restorecon`** (the ENOENT-not-EACCES gotcha), stage into the pool behind a checksum guard
   (skip if the staged volume already matches unless `force_image_rebuild`), emit the image
   sha256. The slow role — gated behind `playbooks/image.yml` / `build_image` tag.

6. **`gdbstub_acl`** — open the `gdbstub_range` + `16514` to `worker_cidr`, drop otherwise:
   `ansible.posix.firewalld` on Fedora/RHEL, `community.general.ufw` on Ubuntu, branched on
   `os_family`. Leaves SSH untouched.

7. **`remote_libvirt_facts`** — render the `[[image]]` + `[[remote_libvirt]]` TOML block
   (with `vcpus`/`memory_mb`, `gdb_addr`, `gdbstub_range`, arch-correct `machine` =
   `pc`|`pseries`) to a controller-side file, and `fetch` the client PEM bundle
   (`cacert/clientcert/clientkey`) into a controller artifacts dir for the operator to load
   into a k8s Secret. The pipeline stops here.

## PKI (`playbooks/pki.yml`, controller-side, `community.crypto`)

Generate one CA on the controller (idempotent — skip if present), sign a per-host server cert
(SAN = host FQDN **and** IP), issue one worker client cert. CA private key + client key stay
controller-side, **ansible-vault-encrypted**, never committed. `pki_mode: byo` skips
generation and installs operator-supplied PEMs instead.

## Config surface (`group_vars/all.yml`)

`storage_pool_target`, `libvirt_network`, `worker_cidr`, `gdbstub_range`, `gdb_addr`,
`base_image_distro`/`_version`, `helper_src` (`deploy/remote-libvirt-guest-helpers/`),
`build_packages`, `vcpus`/`memory_mb` ceilings (required by the `[[remote_libvirt]]` schema),
`force_image_rebuild`, `disable_security_driver`, `pki_mode`, and a `machine_type` map
`{x86_64: pc, ppc64le: pseries}`.

Provider env knobs the inventory model does not carry stay env settings the operator sets on
the deployment: `KDIVE_REMOTE_LIBVIRT_{STORAGE_POOL,NETWORK,MACHINE}`. The roles keep the
host's pool/network names aligned with those values and document them in the emitted facts.

## Distro / arch matrix

| Axis | Ubuntu 26.04 | Fedora 44 | RHEL/Rocky 10 | ppc64le note |
|---|---|---|---|---|
| Pkg manager | `apt` | `dnf` | `dnf` | — |
| qemu pkg | `qemu-system-{x86\|ppc}` | `qemu-system-{x86\|ppc}` | `qemu-kvm` + arch | arch-selected |
| Daemons | modular | modular | modular | same |
| Security driver | AppArmor → `none` | SELinux → `none` | SELinux → `none` | same |
| libguestfs fixes | **yes** (step 4) | no | no | same |
| Firewall | `ufw` | `firewalld` | `firewalld` | same |
| Machine type | `pc` | `pc` | `pc` | `pseries` |

## Idempotency & safety

- Daemon/socket/config changes via handlers; assert `:16514` `LISTEN` after restart.
- `virt-builder` guarded by volume-exists + checksum; pool/net via `community.libvirt`.
- Secrets: CA + client private keys vaulted; artifacts dir gitignored; hosts only ever
  receive the public CA cert + their own server cert.
- `security_driver="none"` is a documented test-host setting (`disable_security_driver` var),
  not silently forced.

## Testing strategy

- **CI (cheap, gates PRs):** `yamllint`, `ansible-lint`, `ansible-playbook --syntax-check`.
- **Acceptance (real hosts):** idempotence run — apply `site.yml` twice against `ub26-big`
  and `fed44-big`; the second run reports **0 changed**. Then `just check-remote-libvirt` and
  the runbook step-8 worker→host TLS connect confirm the path end-to-end.
- **Not used:** Molecule-in-Docker — it cannot exercise KVM or `virt-builder`, so it would
  validate nothing load-bearing.
- ppc64le paths are implemented and reviewed but flagged **unvalidated** until a ppc64le host
  is available.

## Open follow-ups (not this effort)

- k8s Secret + ConfigMap application from the emitted artifacts.
- ppc64le end-to-end validation when hardware exists.
- Optional `kdivectl`-driven registration wrapper consuming `remote_libvirt_facts` output.
