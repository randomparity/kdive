# 0741 — Record the AppArmor session guest confinement limitation

## Status

Accepted (2026-10-07)

- Issue: #3067
- Operator decision: accept documented unconfined posture, retaining failed release evidence.

## Context

Ubuntu 26.04.1 with libvirt 12.0.0-1ubuntu5.5 and AppArmor 5.0.2 runs the
configured unprivileged session daemon under an enforcing daemon profile, but its
capabilities report `none`. A transient paused KVM guest using an existing kernel,
without disks or network, ran `unconfined`; it was then destroyed. The separately
installed provider-authority session endpoint also reports `none`. The existing
system endpoint reports `apparmor` and `dac`; no guest was started there.

The operator cannot open `/sys/kernel/security/apparmor/profiles` despite its
readable file mode. Libvirt's [version 12.0.0 driver](https://github.com/libvirt/libvirt/blob/v12.0.0/src/security/security_apparmor.c)
reads that file in `use_apparmor()` and disables the driver when probing fails.
Dynamic profile creation additionally requires privileged profile management;
[upstream discussion](https://lists.libvirt.org/archives/list/devel@lists.libvirt.org/message/FVYCRGB5EXHJYDDLZZVXDATVRRDBAK3Z/)
identifies this session-mode limitation. Enforcing the daemon profile does not
establish per-domain guest confinement.

## Decision

Accept the existing supported AppArmor session deployment without
per-domain guest confinement, with that limitation explicitly documented. This is
a security-posture decision, not merely permission to correct documentation. It
does not promise a confined multi-user boundary. The operator explicitly accepted
this documented-unconfined resolution. Keep daemon ownership, worker groups,
provider-authority policy and platform support unchanged.

Keep the host-install runner's confinement assertion unchanged: an unconfined
QEMU process fails it. This decision is not passing release evidence and does not
qualify the Debian host-install cell or release 0.5.0. Rerun the complete affected
Debian host-install cell and retain its actual failing confinement evidence. The
failed release confinement obligation remains visible in the release evidence;
closed #2807 is its historical carrier, not an unresolved repair owner. The operator
accepted resolution of #3067 by this documented-unconfined posture; closing that
issue does not satisfy the release confinement obligation.

## Consequences

Documentation and provisioning comments stop claiming AppArmor guest confinement
from the daemon's own profile. Operators can distinguish session behavior from the
system daemon and SELinux session behavior. No code path grants host-root libvirt
access or loads a broader AppArmor profile. A future confined deployment requires
an independently approved authority design and live per-domain enforcement proof.

## Considered & rejected

- **Move fixed workers to the existing system endpoint.** judgment: raw privileged
  libvirt access changes worker authority and shares the host domain namespace;
  this requires a separate security design rather than a URI replacement here.
- **Reuse the installed provider-authority endpoint unchanged.** verified: its
  `virsh capabilities` also reports `none` on the same Ubuntu installation.
- **Force the AppArmor driver or grant profile-management privilege to the
  session daemon.** verified: upstream dynamic-profile management requires
  privileges the session daemon lacks; judgment: adding such authority is a
  security redesign, not a configuration repair.
- **Call an unconfined guest a passing confinement assertion.** judgment: this
  would erase the observed release-proof failure rather than resolve it.
- **Keep the misleading documentation.** verified: the actual guest process was
  `unconfined` while the documentation said session mode confines the domain.
