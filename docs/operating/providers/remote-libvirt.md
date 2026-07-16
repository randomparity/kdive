# remote-libvirt provider

The remote-libvirt provider drives QEMU/KVM guests on a separate target host over a
TLS-secured libvirt connection, so the worker and the guests run on different machines.

> Setting up from scratch? See the [remote-libvirt walkthrough](remote-libvirt-walkthrough.md).

## What it needs

- **TLS PKI.** libvirt's TLS transport authenticates both ends with X.509 certificates. The
  target host serves a CA, server cert, and key; the worker presents a client cert the CA
  signed. The connection URI is the TLS form (for example `qemu+tls://HOST/system`).
- **virtproxyd.** The target runs the modular libvirt proxy daemon listening on the TLS
  port (16514) and forwards to the QEMU driver. The host firewall must permit that port
  from the worker.
- **Guest helpers.** Remote build, install, capture, and in-target artifact transfer use a
  guest agent and a small set of allowlisted in-guest helpers; the base guest image must
  ship them (and the tools they call, such as `tar`, with an SELinux policy that does not
  confine the agent).

All connection settings — the TLS URI, the gdbstub address, credentials — are in
[the config reference](../../guide/reference/config.md).

## Preflight

Check that the provider can reach a target before the first run:

```bash
just check-remote-libvirt HOST USER URI
```

The preflight reports reachability and TLS problems without changing either host.

## Host setup

The [remote-libvirt host setup runbook](../runbooks/remote-libvirt-host-setup.md) covers
provisioning a target host end to end: the PKI, virtproxyd, the firewall ACL, and the guest
image with its helpers.

## Guest CPU advertisement

Remote guests run with a `host-model` CPU (ADR-0297), so the guest ISA tracks the host each
domain lands on. To make that visible before selection, discovery advertises each host's expected
guest CPU as `host_cpu` on `resources.describe` — `{model, vendor, arch, baseline_level}`, where
`baseline_level` is a normalized `x86-64-vN` level (ADR-0368). The CPU a specific System was minted
against is echoed on `systems.get` as `resolved_cpu`.

Two operational notes:

- **Re-register to populate it.** The capabilities row refreshes only on registration, the same as
  `vcpus`/`memory_mb`. A host registered before this feature shipped shows no `host_cpu` until it is
  re-registered (`setup-remote-libvirt` / the reconcile pass over the config overlay); until then
  `resources.describe` omits the field and a new System's `resolved_cpu` is null. This is expected —
  the field degrades to absent, never to a wrong value.
- **It is a registration-time snapshot.** If a host's CPU, microcode, or libvirt changes, re-register
  the host so the advertised `host_cpu` tracks it. `baseline_level` is advisory: a present level is a
  nominal upper bound, not a guaranteed floor, so confirm a hard instruction-set requirement against
  the running guest.
