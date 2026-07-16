# Runbook: POWER (ppc64le) host bring-up for native KVM-HV

Operator guide to bring a POWER ppc64le host from a clean OS install to a running local-libvirt
provider that boots **native ppc64le guests under KVM-HV** (and foreign x86_64 guests under TCG),
then run the full kdive spine against it. This is the native complement to the emulated
[four-method live run](four-method-live-run.md) (which proves the same spine under TCG on an
x86_64 host); the shared worker-host steps are referenced rather than repeated.

This runbook is POWER-generic: it targets any ppc64le KVM-HV host (POWER9 or POWER10). The
`ibm,configure-kernel-dump` fadump path needs QEMU ≥ 10.2 (ADR-0349); everything else works on any
POWER host with `/dev/kvm`. It was validated on a POWER9 (see the proof record
`docs/design/2026-07-15-power-native-kvm-hv-validation-1156-proof-record.md`, #1156).

> The exit criterion for the whole bring-up is a single command:
> `KDIVE_PYTHON="$PWD/.venv/bin/python" ./scripts/check-local-libvirt.sh` printing
> **`local-libvirt host is ready`**. Run it after each step below; it names the exact fix for every
> gap. The steps here are the fixes that check emits on a clean Ubuntu 26.04 ppc64el host.

## 0. Prerequisites

- A ppc64le host with `/dev/kvm` (the `kvm_hv` module loaded). Confirm: `ls -l /dev/kvm` and
  `lsmod | grep kvm_hv`.
- A Rust toolchain on `PATH` ([rustup](https://rustup.rs)) — ppc64le has no prebuilt wheels for
  `pydantic-core`/`libvirt-python`, so `uv sync` builds them from source (AGENTS.md host
  prerequisites).
- `just`: `uv tool install rust-just` (or the distro package).

## 1. Host packages

On Ubuntu 26.04 ppc64el, a clean host is missing the libvirt daemon, the Python/kdump build
headers, and the compose plugin. Install them from a privileged shell:

```bash
sudo apt-get update
# libvirt daemon + client + dev headers, Python headers, the guestfs Python binding
sudo apt-get install -y libvirt-daemon-system libvirt-clients libvirt-dev \
  python3-dev python3-guestfs
# drgn has NO ppc64le wheel -> it builds its vendored libdrgn from source, which needs autotools +
# elfutils headers, and libkdumpfile to read kdump-compressed vmcores (without it, kdump capture
# fails "drgn was built without libkdumpfile support" — see §5).
sudo apt-get install -y autoconf automake libtool pkgconf libelf-dev libdw-dev libkdumpfile-dev
# docker compose v2 plugin (the live-stack backends use it) is not preinstalled
sudo apt-get install -y docker-compose-v2
```

> **Package-name skew from the x86 runbook.** On Ubuntu 26.04 the guestfs Python binding is
> `python3-guestfs` (not `python3-libguestfs`), and it installs to the dpkg path
> `/usr/lib/python3/dist-packages/` (not the `purelib` path the four-method §4b snippet computes) —
> see §4.

## 2. libvirt daemon, groups, network

```bash
# This build ships the MONOLITHIC daemon; virtqemud.socket does not exist here.
sudo systemctl enable --now libvirtd.socket
# Give the operator user access to /dev/kvm and the libvirt socket, then re-login.
sudo usermod -aG libvirt,kvm "$USER"       # log out and back in for the new groups
# SLIRP user-mode networking needs the default libvirt network.
virsh -c qemu:///system net-start default
virsh -c qemu:///system net-autostart default
```

## 3. Worker-host directories and the readable host kernel

Two host constraints from the shared spine (see
[four-method §4b](four-method-live-run.md#prepare-the-worker-host-directories-install-staging--console)),
plus one POWER-specific one:

```bash
# world-traversable staging + console dirs (never under $HOME 0700; the qemu user must traverse them)
sudo install -d -o "$USER" -m 0755 /var/lib/kdive/install /var/lib/kdive/console
# libguestfs builds its supermin appliance from the host kernel. On ppc64le the kernel is
# /boot/vmlinux-* (ELF, no 'z'), shipped 0600 — unreadable by the non-root worker, so build-fs
# and kdump capture fail. Make it readable:
sudo chmod 0644 /boot/vmlinux-*
```

Also pick a world-traversable **build workspace** (not `$HOME`) for `build-fs` — the customization
boot runs a transient domain as the `qemu` user under `qemu:///system`, which cannot traverse a
`0700` `$HOME`. `/var/lib/kdive/build/images` (below) works:

```bash
sudo install -d -o "$USER" -m 0755 /var/lib/kdive/build/images /var/lib/kdive/rootfs/local
```

## 4. Python env (venv + drgn + guestfs binding)

```bash
# uv is not distro-packaged
curl -LsSf https://astral.sh/uv/install.sh | sh    # -> ~/.local/bin
cd ~/src/kdive
uv sync --locked                                   # builds pydantic-core + libvirt-python (Rust)
uv sync --locked --group live                      # builds drgn (autotools + libkdumpfile from §1)
```

Wire the system `guestfs` binding into the venv (the worker imports it for kdump capture). On
Debian/Ubuntu the binding lives at the **dpkg** path, so symlink from there (the four-method §4b
snippet computes the wrong path via `purelib` on Debian/Ubuntu):

```bash
site=$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
ln -sf /usr/lib/python3/dist-packages/guestfs.py "$site"/
ln -sf /usr/lib/python3/dist-packages/libguestfsmod*.so "$site"/
.venv/bin/python -c "import guestfs, drgn"          # both must import
```

Confirm the host is ready (probe the **venv** interpreter the worker uses, not system `python3`):

```bash
KDIVE_PYTHON="$PWD/.venv/bin/python" ./scripts/check-local-libvirt.sh   # -> "local-libvirt host is ready"
```

The remaining WARN (`non-root worker under qemu:///system` cannot read the root-owned console log)
is expected — resolve it by running the worker as **root**, the natural identity for
`qemu:///system` (§6); build and kdump capture work regardless.

## 5. The OIDC issuer on ppc64le (no upstream image)

The live-stack backends come up on ppc64le **except the mock-OIDC issuer**: `postgres`, `minio`,
`minio/mc`, and `prometheus` publish ppc64le manifests, but **`ghcr.io/navikt/mock-oauth2-server`
and `grafana` do not**. `docker compose up` for `oidc` fails
`no matching manifest for linux/ppc64le`.

Do **not** emulate the container — a JVM under qemu-user (`qemu-user-binfmt`) deadlocks on ppc64le
(`java --version` segfaults). Instead run the issuer's **portable JVM bytecode on a native ppc64le
JDK** (the jib image layers are architecture-independent):

```bash
sudo apt-get install -y openjdk-25-jre-headless          # 25 has the --sun-misc-unsafe flag (Java 23+)
cd ~/src/kdive && docker compose stop oidc 2>/dev/null    # free host port 8090 if it was started
cid=$(docker create ghcr.io/navikt/mock-oauth2-server:3.0.3)
docker cp "$cid":/app ~/mock-oidc-app && docker rm "$cid"
cd ~/mock-oidc-app
SERVER_PORT=8090 setsid java --sun-misc-unsafe-memory-access=allow \
  -cp "resources:classes:libs/*" no.nav.security.mock.oauth2.StandaloneMockOAuth2ServerKt \
  </dev/null >/tmp/native-oidc.log 2>&1 &
curl -sf http://127.0.0.1:8090/default/.well-known/openid-configuration >/dev/null && echo "issuer up"
```

Bring up the rest of the backends (skip the obs profile — grafana has no ppc64le image):

```bash
docker compose up -d --wait postgres minio
docker compose run --rm minio-init
./scripts/live-stack/apply-migrations.sh
```

Grafana (obs) is unavailable on ppc64le; run Prometheus alone if you need metrics
(`docker compose up -d prometheus`), or skip observability.

## 6. Build the ppc64le fixture and start the host processes

Build the guest image and extract the boot bundle (fast under native KVM — the customization boot
boots a real ppc64le guest). Run `build-fs` as **root** (the customization boot reads the root-owned
console log to detect its completion marker):

```bash
cd ~/src/kdive
sudo .venv/bin/python -m kdive build-fs --image fedora-kdive-ready-44-ppc64le \
  --workspace /var/lib/kdive/build/images
# -> /var/lib/kdive/rootfs/local/fedora-kdive-ready-44-ppc64le.qcow2
```

Extract the kernel bundle the boot/kdump/fadump proofs upload (the guest's own kernel + initramfs):

```bash
img=/var/lib/kdive/rootfs/local/fedora-kdive-ready-44-ppc64le.qcow2
kver=$(sudo virt-ls -a "$img" /lib/modules | head -1)
work=/var/lib/kdive/bundle-ppc64le
sudo rm -rf "$work"; sudo mkdir -p "$work/stage/boot" "$work/stage/lib/modules"
sudo virt-copy-out -a "$img" /boot/vmlinuz-$kver /boot/initramfs-$kver.img "$work/"
sudo virt-copy-out -a "$img" /lib/modules/$kver "$work/stage/lib/modules/"
sudo cp "$work/vmlinuz-$kver" "$work/stage/boot/vmlinuz"
sudo tar -C "$work/stage" -czf "$work/kernel.tar.gz" boot/vmlinuz lib/modules/$kver
sudo cp "$work/initramfs-$kver.img" "$work/initrd.img"
sudo chown -R "$USER":"$USER" "$work"
```

Start the three host processes as **root** (console/core reads under `qemu:///system`), against the
native issuer from §5. The convenience `scripts/live-stack/up.sh` restarts the `oidc` compose
service, which would clobber the native issuer — so start the processes directly:

```bash
sudo bash -c '
  cd ~/src/kdive && source scripts/live-stack/env.sh
  export KDIVE_GUEST_IMAGE=/var/lib/kdive/rootfs/local/fedora-kdive-ready-44-ppc64le.qcow2
  export KDIVE_KERNEL_SRC=/home/'"$USER"'/src/linux
  for p in server worker reconciler; do
    setsid .venv/bin/python -m kdive "$p" </dev/null >/tmp/kdive-$p.log 2>&1 &
  done'
```

## 7. Run the native spine

```bash
cd ~/src/kdive
set -a; source scripts/live-stack/env.sh
export KDIVE_GUEST_IMAGE_PPC64LE=/var/lib/kdive/rootfs/local/fedora-kdive-ready-44-ppc64le.qcow2
export KDIVE_PPC64LE_BUNDLE=/var/lib/kdive/bundle-ppc64le
export KDIVE_KERNEL_SRC=/home/$USER/src/linux
set +a
# The four #1144/#1146/#1148/#1151 proofs run over the live_stack vehicle; on this POWER host the
# ppc64le guest is NATIVE, so they exercise KVM-HV (accel=kvm) rather than TCG. The proofs assert
# the host-resolved accel (expected_accel, #1156), so they pass under KVM-HV unchanged.
uv run python -m pytest -m live_vm_tcg -o addopts="" -q -rA
```

Expected on native POWER (see the proof records): the ssh-reachability and kdump-capture proofs
pass under KVM-HV. The fadump proof provisions the guest at the **4 GiB** fadump RAM floor
(ADR-0363, #1181) — fadump reserves a boot-memory region on top of `crashkernel`, so at the kdump
proof's 2 GiB the guest fails run-readiness; the floor is enforced at admission, so a fadump profile
below it is rejected `configuration_error` rather than booting to a readiness failure. The
native-POWER fadump crash→capture status and the target host's fadump readiness are recorded in
`docs/design/2026-07-15-power-native-fadump-ram-floor-1181-proof-record.md`.
