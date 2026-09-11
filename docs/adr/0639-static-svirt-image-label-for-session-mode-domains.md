# 0639 — kdive disk images carry a static `svirt_image_t` label; domains declare `relabel='no'`

## Status

Accepted (2026-09-11)

- **Issue:** #2424
- **Amends the host-labeling half of:** the `virt_image_t` guidance in
  `examples/local-libvirt/` (ADR-0204 install staging is untouched)

## Context

The local-libvirt worker drives an operator-owned **session** libvirt daemon
(`qemu+unix:///session?socket=…/virtqemud-sock`). On an SELinux-enforcing RedHat-family host,
every provision failed at domain start with `Could not open '…-overlay.qcow2': Permission denied`.
The host installer had already labeled the tree `virt_image_t` and `restorecon`'d it, so the
static label was present and this was not a DAC mode problem. The denials are in the journal
(`ausearch` does not surface them on these hosts, which is why #2424 recorded "No AVC"):

```
avc: denied { write } comm="qemu-system-x86" name="<system-id>-overlay.qcow2"
     scontext=…:svirt_t:s0:c498,c849 tcontext=system_u:object_r:virt_image_t:s0 tclass=file
avc: denied { map } comm="qemu-system-x86" path="…/<system-id>-baseline/initrd"
     scontext=…:svirt_t:s0:c498,c849 tcontext=system_u:object_r:virt_image_t:s0 tclass=file
```

`svirt_t` may **read** `virt_image_t` but may not **write** or **map** it. `virt_image_t` labels an
image the virtualization stack manages and relabels on demand; `svirt_image_t` is the label a
confined domain may actually use. A **privileged** system daemon closes that gap itself,
relabeling each disk to `svirt_image_t:s0:c<i>,c<j>` at start and restoring it at shutdown — which
is why the Ubuntu/AppArmor and `qemu:///system` paths never saw this.

An **unprivileged** session daemon does not. It still generates a correct dynamic label — the
domain runs as `svirt_t:s0:c681,c997` with `imagelabel svirt_image_t:s0:c681,c997` in the live XML
— but never applies the image relabel, so whatever static label the file carries is what QEMU
meets. That label therefore has to be one `svirt_t` can use.

MCS makes that a one-word change rather than per-domain bookkeeping: the constraint is dominance,
so a domain at `s0:c681,c997` may read *and write* an object at plain `s0`. One static
`svirt_image_t:s0` on the tree serves every System's domain whatever categories libvirt draws.

## Decision

1. Host preparation labels the kdive rootfs tree **`svirt_image_t`**, not `virt_image_t`. This is
   the whole of the functional fix; it covers the overlay's `write` and the direct-kernel
   `initrd`/`kernel` `map` in one rule, because both live under that tree.
2. Every disk kdive renders declares `<seclabel model='selinux' relabel='no'/>` inside its
   `<source>`. kdive owns the image label statically, so this states that contract to libvirt
   instead of leaving it implied by the daemon's privilege level.
3. sVirt stays on. `security_driver` is not touched.

Item 2 changes no behavior on today's session daemon, which already relabels nothing. It is there
for the case that does change behavior: the **shared backing file**. Every System's overlay backs
onto one base image under `rootfs/local/`. A daemon that did relabel would stamp that shared base
with the first domain's categories, and the second System — drawing different categories — would
be denied on a file the first one was using. `relabel='no'` is what keeps a privileged daemon, or
a future session daemon that gains the capability, from converting a working static label into
exactly that cross-System failure.

## Consequences

- Provisioning works on an SELinux-enforcing RedHat-family host with sVirt confinement intact.
  Proven on Fedora 44 and Rocky 10.2; see the spec's Validation section.
- Confinement is unchanged in strength: the domain is still `svirt_t` with per-domain MCS
  categories. What changed is the object label, not the subject's confinement.
- The images are no longer relabeled per boot, so all kdive images share `svirt_image_t:s0`. MCS
  no longer isolates one kdive domain's images from another kdive domain — it never did here,
  since the relabel that would have provided it was not happening. Isolation between kdive and
  **non-kdive** domains on the host is unaffected.
- An already-installed host carries a `virt_image_t` fcontext rule. Re-running the installer must
  replace it; a rule keyed on the same path pattern is not idempotent by presence alone.
- `qemu:///system` deployments (the self-hosted runner, snapshot operations) keep working: a
  privileged daemon relabels from `svirt_image_t` exactly as it did from `virt_image_t`, and
  `relabel='no'` applies only to the disks kdive renders.

## Considered & rejected

- **`security_driver = "none"` in the session daemon config (#2424 option 3).** judgment: a
  permanent loss of sVirt confinement for every kdive domain on every RedHat host, traded for a
  one-word label change. The evidence showed the confinement-preserving fix works, so there is
  nothing to buy with it.
- **Domain-level `<seclabel type='dynamic' model='selinux' relabel='no'/>`.** verified: libvirt
  rejects it — `virsh define` returns `unsupported configuration: dynamic label type must use
  resource relabeling` and `virt-xml-validate` fails the RNG with `Invalid attribute relabel for
  element seclabel` (libvirt 12.0.0, Fedora 44, probed 2026-09-11). `relabel='no'` is only
  expressible per-device, or on a `static` domain label.
- **Per-domain `<seclabel type='static'>` with kdive-generated MCS categories (#2424 option 1,
  first half).** judgment: it makes kdive responsible for allocating unique category pairs and for
  their lifetime across crashes and restarts. libvirt already does that correctly, and a static
  image label reaches the same place without kdive owning a category allocator.
- **Keep `virt_image_t` and rely on libvirt's dynamic relabel.** verified: the session daemon does
  not perform it. After a clean start under enforcing the overlay still read
  `system_u:object_r:svirt_image_t:s0` — the label the tree was given, with no categories added —
  while the domain ran as `svirt_t:s0:c681,c997` (libvirt 12.0.0, Fedora 44, 2026-09-11).
- **A local SELinux policy module granting `svirt_t` write and map on `virt_image_t`.** judgment:
  it widens a host-wide policy rule for every confined domain on the machine in order to avoid
  relabeling one directory kdive owns.
- **Do nothing; document `setenforce 0`.** verified: the denials are `permissive=0` failures under
  the host's shipped policy (`selinux-policy-44.3-1.fc44`, `42.1.18-4.el10`), so this leaves an
  enforcing host — the RedHat-family default — unable to run the project's default provider.
