# 0669 — Supported libguestfs kernel-readability remediation

## Status

Accepted (2026-09-20)

## Context

ADR-0222 maps an unreadable host kernel to `CONFIGURATION_ERROR`, but its remediation says to run
`chmod 0644`, repeat it after upgrades, or use a globbed `dpkg-statoverride`. Existing Debian-family
provisioning instead gives the `kvm` group read access with `root:kvm 0640` and installs a kernel
post-install hook. A globbed statoverride is a literal nonmatching path.

## Decision

Supersede only ADR-0222's kernel-remediation choice. The Debian/Ubuntu operator-visible guidance
leads with the supported local-libvirt provisioning command,
`KDIVE_LIFECYCLE_WITNESS_DATABASE_URL=... just prepare-local-libvirt-host`; another worker deployment
uses its owning provisioning play. Guidance states `root:kvm 0640`, requires the worker to belong to
`kvm`, and says provisioning installs the durable upgrade hook. A labelled one-off fallback uses
`sudo chgrp kvm /boot/vmlinu?-* && sudo chmod 0640 /boot/vmlinu?-*`; it is not durable without
provisioning. The matcher, category, stderr passthrough, and passt remediation from ADR-0222 stay
authoritative.

## Consequences

Operators receive a remedy that matches deployed ownership and avoids world-readable kernels. The
local preflight uses the same durable-versus-one-off distinction. This ADR does not change roles,
hooks, platform scope, or the ephemeral hosted-runner exception.

## Considered & rejected

- **Keep `chmod 0644`.** verified: `deploy/ansible/roles/local_worker_host/tasks/boot_kernels.yml`
  sets `root:kvm 0640` and states that no path may reach `0644`; the wider mode contradicts it.
- **Recommend globbed `dpkg-statoverride`.** verified: ADR-0668 records that statoverrides store
  paths literally, so `/boot/vmlinuz-*` cannot cover future versioned kernel names.
- **Make the local recipe universal.** judgment: remote and other worker deployments own their
  provisioning configuration, so the shared diagnostic must not select a local-only play for them.
