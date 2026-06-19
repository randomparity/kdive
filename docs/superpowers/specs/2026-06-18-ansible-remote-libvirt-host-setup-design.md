# Design: Ansible roles for remote-libvirt host bring-up

**Status:** Implemented + verified on real hardware (Ubuntu 26.04 + Fedora 44, 2026-06-19); x86_64 only, ppc64le unvalidated
**Date:** 2026-06-18 (rev. 2026-06-19 — per-distro daemon model after live findings)
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
`[[remote_libvirt]]`/`[[image]]` block as a controller-side artifact. (The worker client-cert
bundle is a separate controller-side artifact written directly by the PKI play — see PKI
below — never fetched back from the host.)

The default image is **lean**: it serves the provision / `host_dump` capture path out of the
box. A drgn-live or kdump-ready image additionally requires `include_kernel_debuginfo` and the
crashkernel sizing vars set (default off — see `guest_base_image`); the host is "ready to
register" for the provision path, and "ready for live-drgn/kdump" only when those flags are on.

**Out of scope** (left to existing tooling; the roles emit their inputs): creating the
Kubernetes TLS Secret, applying the `systems.toml` ConfigMap into a running cluster, project
onboarding/quota/budget, and ephemeral build-host (`runs.build`) registration.

## Decisions (locked during brainstorm)

| Axis | Decision | Rationale |
|---|---|---|
| Boundary | Host config **+** base image (steps 1–6); emit `systems.toml` block | Produces a host ready for the provision/`host_dump` path without coupling to a live cluster. Live-drgn and kdump additionally need `include_kernel_debuginfo`/crashkernel set (default off) — see `guest_base_image` |
| Daemon model | **Per-distro.** Fedora/RHEL: modular — switch-to-modular (mask `libvirtd*`, enable `virtqemud`/`virtnetworkd`/`virtstoraged`/`virtnodedevd`/`virtsecretd` + `virtproxyd`); TLS via `virtproxyd-tls.socket`. Ubuntu/Debian: monolithic `libvirtd`; TLS via `libvirtd-tls.socket`; never mask | **Verified on Ubuntu 26.04 (2026-06-19): Ubuntu does NOT package the modular daemons** — there is no `virtqemud`/`virtproxyd` binary or socket unit, and the `libvirt-daemon-driver-*` packages are connection-driver plugins for the monolithic `libvirtd`, not standalone daemons. The earlier "modular everywhere" assumption was wrong; masking `libvirtd` on Ubuntu left the host with no libvirt. The runbook's monolithic Ubuntu path is correct. Fedora 44 is modular by default (no `libvirtd` unit exists). |
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
    remote_libvirt_facts/ # emit systems.toml block (client bundle is PKI-play output)
```

`site.yml` runs roles 1, 2, 3, 6, then `remote_libvirt_facts`. The base image (roles 4–5) is
invoked via `playbooks/image.yml` (or a `build_image` tag) because `virt-builder` is slow and
should not run on every converge.

## Roles

Each role has one purpose; all distro/arch divergence is pushed into variables.

1. **`libvirt_stack`** — install virt packages (per-`os_family` lists; qemu chosen by arch:
   `qemu-system-x86` vs `qemu-system-ppc`; plus `python3-libvirt` + `python3-lxml`, which the
   `community.libvirt` modules import on the target — Ubuntu does not pull them in
   transitively). Then set the daemon model **per distro**: on **Fedora/RHEL** run the
   switch-to-modular sub-procedure — stop + `disable` + `mask` the monolithic units
   (`libvirtd.service` and `libvirtd{,-ro,-admin,-tls,-tcp}.socket`), then `enable` the modular
   socket set (`virtqemud.socket`, `virtnetworkd.socket`, `virtstoraged.socket`,
   `virtnodedevd.socket`, `virtsecretd.socket`, `virtproxyd.socket`); on **Ubuntu/Debian** keep
   the monolithic daemon — ensure `libvirtd*` is **not** masked and enable `libvirtd.socket`
   (the modular daemons are not packaged on Ubuntu, so masking `libvirtd` would leave no
   libvirt at all). Add the login user to `kvm`/`libvirt`, assert `virt-host-validate qemu`
   passes the KVM + `/dev/kvm` checks (the assert regex tolerates the single-quoted
   `'/dev/kvm'` device name the tool prints). The TLS socket is enabled by `libvirt_tls`.

2. **`libvirt_tls`** — install the server cert/key + CA cert (from the PKI step) into
   `/etc/pki/{CA,libvirt}`. Resolve the TLS daemon/socket/config-file **per `os_family`**:
   Fedora/RHEL → `virtproxyd` + `/etc/libvirt/virtproxyd.conf` + `virtproxyd-tls.socket`;
   Ubuntu/Debian → `libvirtd` + `/etc/libvirt/libvirtd.conf` + `libvirtd-tls.socket`. Set the
   three listen settings (`listen_tls=1`, `listen_tcp=0`, `auth_tls="none"`) **in place** via
   `lineinfile` in that file (preserving the shipped conf). Security driver is left **on** by
   default: set `security_driver="none"` in `qemu.conf` only when `disable_security_driver`
   (default **`false`**) is opted in per host; the default path keeps the per-distro driver
   (AppArmor/SELinux) and relies on the narrow label fix (correct pool labeling / `virt_use_*`
   booleans) tried first — `none` is the documented last resort. Bind the TLS socket with a
   **stop-daemon-first** step gated on a `:16514` LISTEN probe (libvirt socket activation
   refuses to start a `*-tls.socket` while its daemon is already running, and the probe keeps
   the second run idempotent). **Verification gate:** assert the per-distro **TLS socket unit**
   is `active` and `:16514` is `LISTEN` — check the *socket unit*, not the process name, since
   an idle socket-activated daemon hands the socket back to `systemd` (pid 1).

3. **`libvirt_pool_net`** — define/build/start/autostart the storage pool (`dir`, target a
   var) and the `default` network via `community.libvirt` modules (idempotent).

4. **`guest_image_prereqs`** — the **Ubuntu-only** libguestfs appliance fixes (kernel `0644`,
   `apparmor_restrict_unprivileged_userns=0`, unload `usr.bin.passt`, install
   `isc-dhcp-client`, clear the supermin cache), all `when: ansible_os_family == 'Debian'`.
   No-op on Fedora/RHEL. Image path only.

5. **`guest_base_image`** — build the Fedora base image **natively on the host** (the image is
   always same-arch as its KVM host), so `virt-builder --install`/`--run-command` work on
   ppc64le exactly as on x86_64 (the cross-arch caveat does not apply to a native build). A
   `base_image_source: virt-builder | cloud-image` var (default `virt-builder`) selects the
   build path: the default uses `virt-builder` (arch via `--arch`, Fedora version a var); the
   `cloud-image` fallback downloads a Fedora cloud qcow2 (for the host arch) and applies the
   **same** `virt-customize` package/helper/SELinux steps — the path to use on ppc64le when no
   current `virt-builder` template exists for the pinned Fedora version. Either path installs
   the package set, sets the guest SELinux permissive (unconfined guest agent), and **`--copy-in`
   the three helpers from `deploy/remote-libvirt-guest-helpers/` with `chown root:root` +
   `restorecon`** (the ENOENT-not-EACCES gotcha). The image is **lean by default**: it carries
   the provision/`host_dump` content contract. `include_kernel_debuginfo` (default **`false`**)
   and the crashkernel sizing vars (default off) extend it to a drgn-live/kdump-ready image —
   installing matching `kernel-debuginfo` and setting the crashkernel reservation. This is the
   operator-owned content contract recorded in
   `src/kdive/providers/remote_libvirt/rootfs_build.py` ("matching vmlinux/debuginfo,
   crashkernel-capable kernel"); the helpers also require `tar` in the image —
   `deploy/remote-libvirt-guest-helpers/README.md` documents that the Fedora cloud image carries
   no `tar` by default and `kdive-install-kernel`'s bundle extraction needs it. Stage into the pool behind
   a checksum guard (skip if the staged volume already matches unless `force_image_rebuild`),
   emit the image sha256. The slow role — gated behind `playbooks/image.yml` / `build_image`
   tag. ppc64le paths are flagged **unvalidated** (no ppc64le test host) but are designed-in.

6. **`gdbstub_acl`** — restrict the `gdbstub_range` + `16514` to `worker_cidr` with a
   **default-drop** posture for those ports. On Fedora/RHEL use `ansible.posix.firewalld`
   **rich rules** (`rule family=... source address=<worker_cidr> port port=... protocol=tcp
   accept`) — a plain `port=… enabled` opens the port to the whole zone (any source), so rich
   rules with an explicit `source address` are required. On Ubuntu use `community.general.ufw`
   with the existing ordered `allow from <worker_cidr> to any port ...` ahead of a `deny`. Leaves
   SSH untouched. A **negative verification** confirms the boundary: an off-CIDR source is
   *refused* (not merely that the worker connects).

7. **`remote_libvirt_facts`** — render the staged `[[image]]` + `[[remote_libvirt]]` TOML block
   to a controller-side file, matching `systems.toml.example` exactly: the `[[remote_libvirt]]`
   block emits `name`, `uri`, `gdb_addr`, `gdbstub_range`, the `client_cert_ref` /
   `client_key_ref` / `ca_cert_ref` **filenames** (the SecretRegistry resolves them — never
   secret material), `base_image`, `cost_class`, `concurrent_allocation_cap`, `vcpus`,
   `memory_mb`, and `shapes`; the staged `[[image]]` block emits `provider`, `name`, `arch`,
   `format`, `root_device`, `visibility`, and `[image.source] kind = "staged"` + `volume`. The
   arch-correct `machine` (`pc`|`pseries`) is carried in the emitted facts.
   This role does **not** `fetch` anything from the host — the worker client-cert bundle is
   written controller-side by the PKI play (the host only ever receives its server cert + the CA
   cert). The block is emitted **per host** into a per-host file; because the reconciler currently
   rejects multiple `[[remote_libvirt]]` blocks (`systems.toml.example` singleton constraint),
   only one host's emitted block may be loaded into a given `systems.toml` until multi-instance
   remote selection lands. The pipeline stops here.

## PKI (`playbooks/pki.yml`, controller-side, `community.crypto`)

Generate one CA on the controller (idempotent — skip if present), sign a per-host server cert
(SAN = host FQDN **and** IP), issue one worker client cert. The play writes the worker client
bundle (`cacert`/`clientcert`/`clientkey`) **directly to the controller artifacts dir** — it is
born controller-side and never round-trips through a host, so no `fetch` is involved. Only the
per-host server cert + CA cert are pushed to the host (by `libvirt_tls`). CA private key + client
key stay controller-side, **ansible-vault-encrypted**, never committed. `pki_mode: byo` skips
generation and installs operator-supplied PEMs instead.

## Config surface (`group_vars/all.yml`)

`storage_pool_target`, `libvirt_network`, `worker_cidr`, `gdbstub_range`, `gdb_addr`,
`base_image_distro`/`_version`, `base_image_source` (`virt-builder` default | `cloud-image`
fallback), `helper_src` (`deploy/remote-libvirt-guest-helpers/`), `build_packages`,
`vcpus`/`memory_mb` ceilings (required by the `[[remote_libvirt]]` schema),
`concurrent_allocation_cap`, `shapes`, `cost_class`, `force_image_rebuild`,
`include_kernel_debuginfo` (default **`false`**) + the crashkernel sizing vars (default off),
`disable_security_driver` (default **`false`** — opt-in per host), `pki_mode`, and a
`machine_type` map `{x86_64: pc, ppc64le: pseries}`.

Provider env knobs the inventory model does not carry stay env settings the operator sets on
the deployment: `KDIVE_REMOTE_LIBVIRT_{STORAGE_POOL,NETWORK,MACHINE}`. The roles keep the
host's pool/network names aligned with those values and document them in the emitted facts.

## Distro / arch matrix

| Axis | Ubuntu 26.04 | Fedora 44 | RHEL/Rocky 10 | ppc64le note |
|---|---|---|---|---|
| Pkg manager | `apt` | `dnf` | `dnf` | — |
| qemu pkg | `qemu-system-{x86\|ppc}` | `qemu-system-{x86\|ppc}` | `qemu-kvm` + arch | arch-selected |
| Daemons | **monolithic `libvirtd`** | modular (switch) | modular (switch) | same |
| TLS socket | `libvirtd-tls.socket` | `virtproxyd-tls.socket` | `virtproxyd-tls.socket` | same |
| Security driver | AppArmor → label fix first; `none` opt-in | SELinux → label fix first; `none` opt-in | SELinux → label fix first; `none` opt-in | same |
| libguestfs fixes | **yes** (step 4) | no | no | same |
| Firewall | `ufw` | `firewalld` | `firewalld` | same |
| Image build | `virt-builder` (native) | `virt-builder` (native) | `virt-builder` (native) | `virt-builder` or `cloud-image` fallback, native |
| Machine type | `pc` | `pc` | `pc` | `pseries` |

## Idempotency & safety

- Daemon/socket/config changes via handlers; verify the per-distro **TLS socket unit** is
  `active` *and* `:16514` is `LISTEN` (the socket unit, not the process — an idle
  socket-activated daemon hands the socket back to `systemd`). The TLS socket is bound with a
  stop-daemon-first step gated on a LISTEN probe (socket activation refuses to start a
  `*-tls.socket` while its daemon runs), keeping the second run idempotent.
- Firewall ACL: on **Fedora/RHEL** firewalld is active by default, so the rich-rules enforce
  immediately (verified: off-CIDR refused, in-CIDR allowed). On **Ubuntu** the role stages the
  ufw allow/deny rules but does **not** enable ufw — enabling it changes the host's whole
  inbound posture and needs an explicit SSH allow first, so on a host with ufw inactive the
  gdbstub ACL is staged-but-inert until the operator enables ufw deliberately.
- `virt-builder` guarded by volume-exists + checksum; pool/net via `community.libvirt`.
- Secrets: CA + client private keys vaulted; artifacts dir gitignored; hosts only ever
  receive the public CA cert + their own server cert (no host→controller `fetch`).
- The host security driver stays **on** by default. `security_driver="none"` is opt-in only
  (`disable_security_driver`, default `false`), tried after the narrow SELinux/AppArmor label
  fix (pool labeling / `virt_use_*` booleans) — never silently forced.
- Read-only / assert / command tasks (`virt-host-validate`, the `:16514` + socket-ownership
  assert, the off-CIDR refusal check, supermin/cache clears) set `changed_when:` honestly so the
  "second run = 0 changed" idempotence bar measures real drift, not command-task noise.

## Testing strategy

- **CI (cheap, gates PRs):** a new `just lint-ansible` recipe (`yamllint` + `ansible-lint` over
  the explicit `deploy/ansible` path, mirroring `just lint-shell` at `justfile:142`) plus
  `ansible-playbook --syntax-check`. The gate has **three** wiring points that all ship together,
  because hosted CI calls `just lint`/`type`/`test` *separately* and never `just ci` — so a gate
  added only to the `ci` recipe would not run on PRs: (1) the `lint-ansible` recipe in `justfile`,
  (2) a matching `.pre-commit-config.yaml` hook, and (3) an explicit step in
  `.github/workflows/ci.yml` (alongside the existing `just lint-shell` step). These three are
  named here as implementation obligations.
- **Idempotence bar:** read-only/assert/command tasks set `changed_when:` honestly (above), so
  the second-run-0-changed measure reflects real drift, not command-task noise.
- **Acceptance (real hosts) — DONE 2026-06-19** on `ub26-big.dev` (Ubuntu 26.04, monolithic)
  and `fed44-big.dev` (Fedora 44, modular): `site.yml` applied, the **second run reported 0
  changed** on both, `:16514` was served by the right socket unit, and a worker→host **mutual
  TLS handshake succeeded** with the generated PKI. firewalld enforced the ACL on Fedora
  (off-CIDR refused, in-CIDR allowed); the ufw ACL on Ubuntu is staged-but-inert (ufw not
  enabled). Four runtime bugs that the lint/syntax gates could not catch were found and fixed
  during this run (removed `yaml` callback; ufw global-deny → port-specific; KVM-assert quote;
  the per-distro daemon model itself), plus the `python3-libvirt`/`python3-lxml` target deps.
- **Structural test:** a `tests/deploy/` test (mirroring `tests/deploy/test_systemd_units.py`)
  asserts the rendered `systems.toml` block carries the full `systems.toml.example` field set and
  validates the role-var surface.
- **Not used:** Molecule-in-Docker — it cannot exercise KVM or `virt-builder`, so it would
  validate nothing load-bearing.
- ppc64le paths are implemented and reviewed but flagged **unvalidated** until a ppc64le host
  is available.

## Deliverables (beyond the roles)

- `deploy/ansible/README.md` (mirroring the `deploy/*/README.md` convention).
- The `just lint-ansible` recipe + `.pre-commit-config.yaml` hook + `ci.yml` step (above).
- The `tests/deploy/` structural test (above).

## Open follow-ups (not this effort)

- k8s Secret + ConfigMap application from the emitted artifacts.
- ppc64le end-to-end validation when hardware exists.
- Optional `kdivectl`-driven registration wrapper consuming `remote_libvirt_facts` output.
