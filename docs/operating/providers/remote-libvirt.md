# Remote-libvirt provider

The remote-libvirt provider runs QEMU/KVM guests on a separate Linux host. The KDIVE worker
connects over mutually authenticated libvirt TLS; it needs no local KVM. Remote-libvirt is
opt-in through a `[[remote_libvirt]]` instance in the `systems.toml` inventory.

## Setup path

1. [Prepare the target host](../runbooks/remote-libvirt-host-setup.md): configure virtualization,
   TLS, storage, firewall rules, and the base guest image.
2. Choose a control plane: the [Helm deployment runbook](../runbooks/kubernetes-deploy.md) covers
   Kubernetes; the [live-stack runbook](../runbooks/live-stack.md) covers host processes for tests.
3. [Register the target](../runbooks/remote-libvirt-host-setup.md#3-register-remote-libvirt-on-the-deployment)
   through the inventory and mount the TLS secret files on the worker.
4. [Onboard the project](../project-onboarding.md) with a budget and quota.
5. For the host-process test stack, run the [remote lifecycle tests](../runbooks/remote-live-stack.md).
   These tests intentionally crash disposable guests.

The [configuration reference](../../guide/reference/config.md) owns runtime settings. The
host-setup runbook owns inventory examples and secret mounting instructions.

## Connection and image requirements

- **Mutual TLS:** the host presents a server certificate, and the worker presents a client
  certificate, both verified against their trusted CA. Use a `qemu+tls://HOST/system` URI.
  Distribute the CA certificate to the worker; keep the CA signing key outside the runtime.
- **Libvirt listener:** the target exposes its TLS listener, usually port 16514, through
  `libvirtd` or the modular `virtproxyd` daemon. Permit connections from the worker.
- **Debug and optional SSH access:** these use separate ports, outside the TLS connection.
  Follow the [remote stack's firewall requirements](../runbooks/remote-live-stack.md#2-the-gdbstub-port-acl).
- **Guest image:** enable qemu-guest-agent and install the allowlisted helpers for kernel
  installation, capture, and artifact transfer. The [host setup procedure](../runbooks/remote-libvirt-host-setup.md)
  covers their packages and guest policy. Kernel compilation happens outside KDIVE;
  follow the [external-build upload guide](../external-build-upload.md).
- **Object store:** the guest must reach the endpoint used in presigned kernel-download and
  vmcore-upload URLs. Worker-only reachability is insufficient.

## Preflight and CPU expectations

From a checkout on the connecting host, run `just check-remote-libvirt HOST USER URI`. It checks
SSH, local PKI/helper files, and a libvirt connection; it does not verify helpers inside a guest
or prove the debug-port ACL. The SSH probe accepts new host keys into the local known-hosts file.

Remote guests use a `host-model` CPU (ADR-0297). Verify a hard instruction-set requirement in the
running guest. The remote inventory path does not populate `host_cpu` through live discovery;
`systems.get` can therefore return a null `resolved_cpu`. Neither inventory reconciliation nor
`just setup-remote-libvirt` is a CPU-discovery refresh command.
