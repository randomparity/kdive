# Runbook: self-hosted Ubuntu 26.04 KVM runner

Bring up a self-hosted GitHub Actions runner for the native-KVM `live_vm` tier
(epic #1289 sub-issue B, [ADR-0387](../../adr/0387-selfhosted-kvm-runner-host-codification.md)).
The host is codified as Ansible roles under `deploy/ansible`; this runbook is the
operator walkthrough. Every step is arch-parameterized — a `[self-hosted, kvm,
ppc64le]` POWER runner is the same procedure with the runner-binary override.

## What it produces

An Ubuntu 26.04 LTS host that satisfies the sub-issue A `live_vm` environment
contract (`tests/live_vm/__init__.py`): `/dev/kvm` + native qemu, the monolithic
`libvirtd`, the `default` pool/network, the kernel-debug toolchain (`crash`,
`makedumpfile`, `kexec-tools`, `kdump-tools`, `gdb`, `python3-guestfs`), a
world-traversable staging tree (AppArmor-confined, no static label), a short
`XDG_RUNTIME_DIR`, and a registered — but **not yet listening** — GitHub Actions
runner service. Ubuntu 26.04 is the base because its system Python 3.14 has a
matching `python3-guestfs` binding (see ADR-0387).

## Roles and playbook

`deploy/ansible/playbooks/runner.yml` applies, in order:

1. `libvirt_stack` (reused) — qemu/libvirt/libguestfs, monolithic `libvirtd` on Ubuntu, KVM assertion.
2. `libvirt_pool_net` (reused) — the `default` dir pool + network.
3. `live_vm_host` — the contract delta: service-account groups, the toolchain,
   `/boot` kernel readability, the persistent venv, both staging dirs
   (AppArmor-confined), `enable-linger`, and the two-part host-contract gate.
4. `github_runner` — the runner asset (checksum-verified), registration, and the
   systemd service (installed stopped).

## Prerequisites

- Ubuntu 26.04 LTS host (x86_64; ppc64le is a drop-in — see below), SSH-reachable
  as a `become`-capable account. On a freshly-provisioned host, wait for first-boot
  `unattended-upgrades` to release the dpkg lock (`sudo cloud-init status --wait`,
  or until `fuser /var/lib/dpkg/lock-frontend` is silent) before the first run —
  otherwise the apt install can fail acquiring the frontend lock.
- On the control machine: `uv` and the collections —

  ```sh
  cd deploy/ansible
  ansible-galaxy collection install -r requirements.yml
  ```

- The host entry: add the host under the `live_vm_runners` group in
  `inventory/hosts.yml` and give it a `host_vars/<host>.yml` (see
  `host_vars/ub26-runner.yml`) with its `ansible_host` and
  `github_runner_repo_url`.

## The persistent venv and the D contract

`live_vm_host` provisions a persistent project checkout + venv at `live_vm_venv`
(default `/opt/kdive`), built against the **system** interpreter (Ubuntu 26.04's
Python 3.14: `uv sync --python /usr/bin/python3 --group live`, which builds
`drgn`+`libvirt-python` from PyPI) so the symlinked `libguestfs` native module
ABI-matches. The `live_vm` CI job (sub-issue D) **must** reuse this
venv via `KDIVE_PYTHON=<live_vm_venv>/.venv/bin/python` — it must not build a
throwaway per-job venv in `$GITHUB_WORKSPACE`, which would have `drgn` but not the
`libguestfs` symlinks, so `import guestfs` would fail at live-test time.

## Bring-up (ordered — the security steps come before the runner listens)

1. **Provision the host contract** (no token needed for the host roles):

   ```sh
   cd deploy/ansible
   ansible-playbook playbooks/runner.yml --limit <host> \
     -e github_runner_registration_token=<token>
   ```

   Obtain `<token>` from the repo/org runner settings
   (`Settings -> Actions -> Runners -> New self-hosted runner` shows a
   short-lived registration token). It is `no_log` and must be passed at runtime,
   never committed. The runner registers but the service is installed **stopped**.

2. **Apply the trusted-events posture BEFORE enabling the service.** A listening
   self-hosted runner plus a fork pull request is arbitrary code execution on the
   host, so do not enable the service until:
   - the repository setting **Settings -> Actions -> General -> Fork pull request
     workflows -> "Require approval for all outside collaborators"** (or stricter)
     is applied, and
   - sub-issue D's `live-vm` workflow exists with its `if:` guard restricting the
     job to `schedule` / `workflow_dispatch` (never fork PRs).

3. **Wire the object-store secrets.** Add `KDIVE_S3_*` as repo/organization
   secrets (readable by `schedule` / `workflow_dispatch`, never by fork PRs). B
   sets only the `KDIVE_SECRETS_ROOT` pointer in the runner `.env`; the S3
   credential **material** under that root is placed by sub-issue C/D or the
   operator (sub-issue A's resolver checks the S3 endpoint/bucket env, not the
   credential files).

4. **Enable the runner** once the posture is in place:

   ```sh
   ansible-playbook playbooks/runner.yml --limit <host> \
     -e github_runner_service_enabled=true \
     -e github_runner_registration_token=<token>
   ```

   Confirm the runner shows **Idle/online** in the repo runner list.

## ppc64le runner (drop-in)

`actions/runner` ships no ppc64le release asset. Build one from
`actions/runner` for ppc64le, then set `github_runner_tarball_url` (and a pinned
`github_runner_sha256`) in the host's `host_vars`; the rest of the host build is
unchanged. With neither an upstream asset nor an override URL, the role fails loud
naming the gap rather than downloading a wrong-arch binary.

## Guest-image stores and disk budget

The live tiers boot a rootfs, its kernel, and matching `vmlinux` debuginfo. Two
scripts produce these (#1292, [ADR-0388](../../adr/0388-guest-image-debuginfo-provisioning.md)),
as two deliberately-separate stores:

- **Self-hosted warm store** — `scripts/live-vm/warm-store.sh`, persistent at
  `KDIVE_WARM_STORE_DIR` (default `/var/lib/kdive/warm-store`, the dir
  `live_vm_host` creates). Idempotent: it rebuilds only when the pinned kernel
  changes or a staged file fails its recorded digest, and otherwise reuses the
  warm set. It only **reports** usage — the host's own disk is not budget-gated.
- **Hosted TCG set** — `scripts/live-vm/stage-tcg-images.sh`, ephemeral on the
  hosted runner's `/mnt` scratch (`KDIVE_TCG_STAGE_DIR`, default
  `/mnt/kdive-tcg`). It fetches debuginfo on demand and **enforces** a disk
  budget: a pre-stage free-space check for the whole budget, then a post-stage
  footprint cap (`KDIVE_TCG_BUDGET_BYTES`, default ~7 GB), each failing loud.

Both fetch debuginfo by the kernel's build-id via `debuginfod-find`, so they
require `DEBUGINFOD_URLS` pointing at a server that indexes **the guest kernel's**
debuginfo — the *guest* distro's debuginfod, not the runner's (e.g. a Fedora
guest → `https://debuginfod.fedoraproject.org`).

**Host tool prerequisites.** The scripts preflight these and fail loud (naming the
package) if any is absent: `virt-ls`/`virt-copy-out` (libguestfs-tools),
`eu-readelf` (`elfutils`), and `debuginfod-find` (`debuginfod`) — the last two are
in the `live_vm_host` role package set. A **compressed** guest kernel (x86 bzImage)
additionally needs `extract-vmlinux` on `PATH`; it is not a standalone package —
it ships in the kernel source scripts, so symlink it (per running kernel):
`sudo ln -sf "/usr/src/linux-headers-$(uname -r)/scripts/extract-vmlinux" /usr/local/bin/`.
A bare-`vmlinux`-ELF guest kernel (ppc64le pseries) does not need it.

### Disk budget

| Component | Derived | Measured (Fedora 44 x86_64, see below) | Gate |
| --- | --- | --- | --- |
| rootfs qcow2 | ~2 GB | 1.4 GB | staged-set cap |
| kernel (`vmlinux`) | ~0.1 GB | 18 MB | staged-set cap |
| matching `vmlinux` debuginfo | ~1.2 GB | 488 MB | staged-set cap |
| transient debuginfod cache copy | ~1.2 GB | ~488 MB (freed after `mv`) | pre-stage free-space |
| kdump/vmcore + working headroom | ~2 GB | (run-time, guest-RAM-dependent) | pre-stage free-space |
| **produced set total** | — | **1.80 GB** | — |
| **whole `/mnt` budget (TCG)** | **~7 GB** | (headroom generous) | `require_free_space` (pre-stage) |

The ~2 GB vmcore headroom assumes a guest of ≤~2 GB RAM (a vmcore scales with
populated guest memory); raise `KDIVE_TCG_BUDGET_BYTES` if the guest RAM rises.
The scripts print the measured actual on stderr (the `live-vm usage:` line).

**Live proof (2026-07-19, Ubuntu 26.04 runner, `github-runner`, KVM).** The real
pipeline — `build-fs fedora-kdive-ready-44` (session mode) → `/boot` kernel
extract → `debuginfod-find` from `debuginfod.fedoraproject.org` — produced kernel
`6.19.10-300.fc44.x86_64` with build-id `ac46f500…`, and the fetched debuginfo
carried the **same** build-id (the match-by-construction guarantee, proven on real
artifacts). The scripts' `require_tools` preflight and the missing-pin `die` were
confirmed to fail loud on the same host.

### Operational prerequisites for the warm-store refresh

`warm-store.sh` invokes `build-fs`, whose customize-via-boot step (ADR-0345) boots
the guest under libvirt. On the runner that requires, beyond the tools above:

- **`e2fsprogs`** (`tune2fs`/`mkfs.ext4`) on `PATH` including `/usr/sbin` — a
  `build-fs` dependency for the ext4 repack.
- **`KDIVE_LIBVIRT_URI=qemu:///session`** so qemu runs as the runner user: the
  default `qemu:///system` writes a root-owned console log the non-root runner
  cannot read (the root-readback wall), and its qemu (uid `libvirt-qemu`) cannot
  traverse the runner-owned build workspace. Session mode sidesteps both; keep
  `XDG_RUNTIME_DIR` short (e.g. `/run/user/<uid>`) for the QMP socket path.

### Producing each store (operator)

The pins are supplied inputs (the operator/CI compute the NVR from the base
image; the scripts run no live distro query), and an unset input fails loud:

```sh
# Self-hosted warm store (native KVM), run as the runner service account:
DEBUGINFOD_URLS=<distro-debuginfod> KDIVE_LIBVIRT_URI=qemu:///session \
  KDIVE_PYTHON=<venv>/bin/python \
  KDIVE_WARM_STORE_TARGET_NVR=<pinned-kernel-nvr> \
  KDIVE_WARM_STORE_IMAGE=<catalog-rootfs-image> \
  scripts/live-vm/warm-store.sh

# Hosted TCG set (on the runner's /mnt):
DEBUGINFOD_URLS=<distro-debuginfod> KDIVE_TCG_IMAGE=<ppc64le-rootfs-image> \
  scripts/live-vm/stage-tcg-images.sh
```

Each prints the eval-safe `KDIVE_LIVE_VM_ROOTFS` / `KDIVE_LIVE_VM_BZIMAGE` /
`KDIVE_LIVE_VM_VMLINUX` wiring block on stdout for the boot step to consume.
The warm-store refresh holds an exclusive `flock` on `<store>/.lock`; the
consuming boot (sub-issue D) takes a **shared** lock on the same file so a
refresh cannot swap the artifacts out from under an in-flight domain.

## Maintenance

- **After a kernel upgrade**, re-run `playbooks/runner.yml` (which re-applies
  `0640 root:kvm` to `/boot/vmlinuz-*`): a new kernel ships `0600 root:root`,
  which fails the libguestfs appliance build for the non-root runner user.
- **Version bump:** update `github_runner_version` and `github_runner_sha256`
  together (the linux-x64 SHA-256 is published in the `actions/runner` release
  notes between the `BEGIN/END SHA linux-x64` markers, not a fetchable sidecar).
- **Stale registration:** GitHub auto-removes a runner left offline past its
  window (~14 days). If the runner was registered then left stopped past that
  window, the server-side registration is gone while the local `.runner` markers
  persist. Recover with `./config.sh remove --token <fresh-token>` in
  `github_runner_install_dir`, then re-run the bring-up with a fresh token.

## Verification

- **Idempotence:** run `playbooks/runner.yml --limit <host>` a second time on a
  converged, already-registered host — it reports **0 changed** (the host roles
  are idempotent and the `.runner` marker skips re-registration).
- **Host contract:** `live_vm_host`'s gate runs `scripts/check-local-libvirt.sh`
  as the runner user and asserts `/boot` readability, group membership, and
  `/run/user/<uid>` (no static disk label — AppArmor's `virt-aa-helper` confines
  qemu dynamically); the play fails if the host is not ready.
- **Deregister / teardown:** `./config.sh remove --token <token>` then
  `sudo ./svc.sh uninstall` in `github_runner_install_dir`; removing the host from
  `live_vm_runners` leaves the remote-libvirt automation untouched.
