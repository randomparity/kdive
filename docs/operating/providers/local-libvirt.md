# local-libvirt provider

The local-libvirt provider runs KDIVE's build, boot, debug, and crash-capture work on the
same host as the worker, driving QEMU/KVM guests through libvirt.

> Setting up from scratch? See the [local-libvirt walkthrough](local-libvirt-walkthrough.md).

## What it needs

- A working libvirt with the QEMU/KVM stack and an accessible socket (the worker connects
  to `qemu:///system` by default, or `qemu:///session` for an unprivileged session).
- Hardware virtualization (`/dev/kvm`) for usable boot performance.
- A kernel source tree for builds (`KDIVE_KERNEL_SRC`) and disk space for guest overlays,
  artifacts, and captured vmcores.
- The toolchain the build path invokes: `make`, a C compiler (`gcc`/`binutils`), and the
  kernel-build dependencies `flex`, `bison`, `bc`, `git`, `rsync`, `xz`, and the
  `libssl`/`libelf` development headers. On this venv-on-a-host deployment the operator
  installs them; the container worker image bundles them (ADR-0146).

All host-facing settings are in [the config reference](../../guide/reference/config.md).

## Architecture and acceleration

The provider runs on both `x86_64` and `ppc64le` (POWER9/POWER10) hosts. A guest whose arch
matches the host runs under **KVM**; a foreign-arch guest runs under **TCG** software emulation
(roughly 10× slower). So an `x86_64` host runs `x86_64` guests native and can run `ppc64le`
guests under TCG, while a POWER host runs `ppc64le` guests native under KVM-HV. The domain XML
— machine type, console device, CPU model — is derived from the profile arch, so the same
lifecycle drives both; see
[Cross-architecture guests](../install.md#cross-architecture-guests) for the per-arch QEMU
emulator packages and the accelerator diagnostics.

Because TCG guests boot far slower than KVM guests, the provider scales boot-readiness
deadlines for them by `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER` (default `10.0`; set `1.0` to
disable). KVM guests are never scaled.

**ppc64le host build note.** A venv-on-host deployment on a POWER host builds a few wheel-less
dependencies (`pydantic-core`, and the `just`/`prek` CLIs) from source, so a **Rust toolchain**
(`rustc`/`cargo`, via [rustup](https://rustup.rs)) must be on `PATH` before `uv sync`; `x86_64`
needs none. `scripts/check-setup-deps.sh` is arch-aware and prints the exact rustup hint when
it is missing ([ADR-0360](../../adr/0360-arch-aware-rust-dep-check.md)); the runtime container
image needs no Rust on either arch. Full detail is in the
[cross-platform development guide](../../development/cross-platform.md).

## Preflight

Before the first run, check the host with:

```bash
just check-local-libvirt
```

The preflight reports missing pieces — libvirt reachability, `/dev/kvm`, the toolchain —
without changing the host.

## Fixture-profile override

A fixture **profile** is the build-time kernel-config/cmdline validation policy a built
kernel is checked against — the required `CONFIG_*` options, the required and protected
cmdline tokens, and the rootfs shape. It is provider-scoped shared policy (keyed
`provider/name/arch`), non-secret, and not tied to any one System. The packaged default is
`console-ready_x86_64`. The catalog is read from disk, so an operator can override it without
rebuilding the image (ADR-0120) — there is no DB or object-store catalog to publish into.

`KDIVE_FIXTURE_CATALOG_PATH` points the loader at an operator-owned catalog directory. The
image never writes that path, so an override survives a redeploy.

**venv-on-host.** Copy the packaged default into a directory you own, edit it, and point the
processes at it:

```bash
python -m kdive install-fixtures --dest /etc/kdive/fixtures/local-libvirt
# edit /etc/kdive/fixtures/local-libvirt/profiles/console-ready_x86_64.yaml
export KDIVE_FIXTURE_CATALOG_PATH=/etc/kdive/fixtures/local-libvirt
```

Set `KDIVE_FIXTURE_CATALOG_PATH` identically for **every** process that loads the catalog —
`server`, `worker`, and `reconciler` each read their own environment.

**Kubernetes.** Supply the catalog as a **flat-layout** ConfigMap and set
`fixtures.configMapName` in the chart. A ConfigMap key cannot contain `/`, so the manifest and
each profile YAML are top-level keys and the manifest references profiles by **bare filename**:

```bash
# manifest.yaml must list the profile by bare filename: profiles: ["console-ready_x86_64.yaml"]
kubectl create configmap kdive-fixtures \
  --from-file=manifest.yaml --from-file=console-ready_x86_64.yaml
helm upgrade ... --set fixtures.configMapName=kdive-fixtures
```

The chart mounts the ConfigMap on the server/worker/reconciler pods (not the migrate job,
which does not read the catalog) and sets `KDIVE_FIXTURE_CATALOG_PATH` to the mount path.

**Verify.** After overriding or mounting, call the `fixtures.validate` tool. It reports the
resolved path and the `(provider, name, arch)` profiles the catalog advertises, or a
`configuration_error` if the catalog is absent or malformed — so a bad override is caught
before a build depends on it. It attests the **server** process's view; in venv-on-host make
sure the worker and reconciler resolve the same path, while the k8s ConfigMap mounts on every
component pod.

## End to end

The [live-stack runbook](../runbooks/live-stack.md) walks a full build → boot → verify
cycle over the local provider, including the backend bring-up and the MCP-driven flow.
