# local-libvirt provider

The local-libvirt provider runs KDIVE's build, boot, debug, and crash-capture work on the
same host as the worker, driving QEMU/KVM guests through libvirt.

## What it needs

- A working libvirt with the QEMU/KVM stack and an accessible socket (the worker connects
  to `qemu:///system` by default, or `qemu:///session` for an unprivileged session).
- Hardware virtualization (`/dev/kvm`) for usable boot performance.
- A kernel source tree for builds (`KDIVE_KERNEL_SRC`) and disk space for guest overlays,
  artifacts, and captured vmcores.
- The toolchain the build path invokes (`make`, a compiler, and the usual kernel build
  dependencies).

All host-facing settings are in [the config reference](../../guide/reference/config.md).

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
