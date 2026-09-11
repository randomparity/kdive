# 0639 — kdive disk images carry a static `svirt_image_t` label

## Status

Proposed

- **Issue:** #2424
- Opens Proposed on the design commit and flips to Accepted in the commit that implements it,
  per the ratification rule in `docs/adr/README.md`.

## Context

The local-libvirt worker can drive an operator-owned **session** libvirt daemon
(`qemu+unix:///session?socket=…/virtqemud-sock`), which is what the `examples/local-libvirt`
stack installs. On an SELinux-enforcing RedHat-family host, every provision failed at domain start
with `Could not open '…-overlay.qcow2': Permission denied`. The host installer had already labeled
the tree `virt_image_t` and `restorecon`'d it, so the static label was present and this was not a
DAC mode problem. The denials are in the journal (`ausearch` does not surface them on these hosts,
which is why #2424 recorded "No AVC"):

```
avc: denied { write } comm="qemu-system-x86" name="<system-id>-overlay.qcow2"
     scontext=…:svirt_t:s0:c498,c849 tcontext=system_u:object_r:virt_image_t:s0 tclass=file
avc: denied { map } comm="qemu-system-x86" path="…/<system-id>-baseline/initrd"
     scontext=…:svirt_t:s0:c498,c849 tcontext=system_u:object_r:virt_image_t:s0 tclass=file
```

`svirt_t` may **read** `virt_image_t` but may not **write** or **map** it. `virt_image_t` labels an
image the virtualization stack manages and relabels on demand; `svirt_image_t` is the label a
confined domain may actually use. A **privileged** daemon closes that gap itself, relabeling each
disk at start and restoring it at shutdown — which is why `qemu:///system` (kdive's default
`KDIVE_LIBVIRT_URI`) and the Ubuntu/AppArmor path never saw this.

An **unprivileged** session daemon does not. It still generates a correct dynamic label — the
domain runs as `svirt_t:s0:c681,c997` with `imagelabel svirt_image_t:s0:c681,c997` in the live XML
— but never applies the image relabel, so whatever static label the file carries is what QEMU
meets. That label therefore has to be one `svirt_t` can use.

MCS makes that a one-word change rather than per-domain bookkeeping: the constraint is dominance,
so a domain at `s0:c681,c997` may read *and write* an object at plain `s0`. One static
`svirt_image_t:s0` serves every domain whatever categories libvirt draws.

## Decision

1. Host preparation labels the kdive image directories **`svirt_image_t`**, not `virt_image_t`:
   the rootfs tree (`/var/lib/kdive/rootfs`, and the nested `local/` rule `build-image.sh` owns)
   and the install-staging root (`/var/lib/kdive/install`), whose `kernel`/`initrd` the install
   plane points a live domain's `<os>` at.
2. sVirt stays on. `security_driver` is not touched, and no domain XML declares a security label.

The label is chosen so that it is correct under **both** daemons. A privileged daemon relabels
from `svirt_image_t` exactly as it relabelled from `virt_image_t`, so the default deployment is
unaffected; an unprivileged one relabels nothing and meets a label it can use.

## Consequences

- Provisioning is expected to succeed on an SELinux-enforcing RedHat-family host with sVirt
  confinement intact. The end-to-end proof on Fedora 44 and Rocky 10.2 is a completion criterion
  of #2424 and is recorded in the implementing PR, not here.
- Confinement is unchanged in strength: the domain is still `svirt_t` with per-domain MCS
  categories. What changed is the object label, not the subject's confinement.
- Under the session daemon the images are not relabeled per boot, so all kdive images share
  `svirt_image_t:s0`. MCS does not isolate one kdive domain's images from another's there — it
  never did, since the relabel that would provide it was not happening. Isolation between kdive
  and **non-kdive** domains is unaffected, and under a privileged daemon per-domain categories
  still apply.
- kdive installs **two** fcontext rules on the rootfs tree — `install-host.sh` owns
  `/var/lib/kdive/rootfs(/.*)?` and `build-image.sh` owns the nested
  `/var/lib/kdive/rootfs/local(/.*)?` — plus one for the install-staging root. A host installed
  before this record carries the old type on whichever of those it already has, so the labeling
  helper migrates an existing rule rather than skipping it, and `install-host.sh` migrates the
  nested rule too so that re-running it alone is sufficient.
- Rolling the label back needs `restorecon -R -F`: `svirt_image_t` is listed in
  `/etc/selinux/targeted/contexts/customizable_types` and `virt_image_t` is not, so a plain
  `restorecon` relabels *into* the new type but silently skips relabeling *out* of it.

## Considered & rejected

- **`security_driver = "none"` in the session daemon config (#2424 option 3).** judgment: a
  permanent loss of sVirt confinement for every kdive domain on every RedHat host, traded for a
  one-word label change. The confinement-preserving fix works, so there is nothing to buy with it.
- **Declare `<seclabel … relabel='no'/>` on the rendered disks (#2424 option 1).** verified: three
  results retire it. (a) The domain-level form is rejected outright — `virsh define` returns
  `unsupported configuration: dynamic label type must use resource relabeling` and
  `virt-xml-validate` fails the RNG (libvirt 12.0.0, Fedora 44, 2026-09-11). (b) Its purpose would
  have been to stop a privileged daemon stamping a *shared backing image* with one domain's MCS
  categories, but libvirt already handles that: with two domains running against one base,
  `base.qcow2` was labeled `system_u:object_r:virt_content_t:s0` while the overlays took
  `svirt_image_t:s0:c352,c669` and `svirt_image_t:s0:c425,c564` (same host and date). (c)
  `KDIVE_LIBVIRT_URI` defaults to `qemu:///system`
  (`src/kdive/providers/local_libvirt/settings.py:55`), so the declaration would have landed on
  the default deployment and disabled the relabel that makes it work — including the
  customization boot, whose dependence on that relabel is recorded at
  `src/kdive/providers/local_libvirt/rootfs_build.py:233-244`.
- **Per-domain `<seclabel type='static'>` with kdive-generated MCS categories.** judgment: it
  makes kdive responsible for allocating unique category pairs and for their lifetime across
  crashes and restarts. libvirt already does that correctly.
- **Relabel from the worker at overlay-creation time,** as `prepare_pcap_dir` already does through
  `_relabel_svirt_image` (`src/kdive/providers/shared/runtime_paths.py:35-44`, ADR-0385).
  verified: that helper is documented as working only for a **root** worker — "The root worker
  prepares it automatically; run the worker as root or provision the directory out of band" — and
  the example stack's worker is a fixed unprivileged account, which lacks the `relabelto`
  permission. It also leaves a host that has never run a provision unlabeled, so host preparation
  has to carry the rule regardless.
- **Keep `virt_image_t` and rely on libvirt's dynamic relabel.** verified: the session daemon does
  not perform it. Under `virt_image_t` the domain started as `svirt_t:s0:c681,c997` while the
  overlay stayed `virt_image_t:s0` and QEMU was denied `write` on it — the Context AVCs above, on
  libvirt 12.0.0 / Fedora 44.
- **Do nothing; document `setenforce 0`.** verified: the denials are `permissive=0` failures under
  the hosts' shipped policy (`selinux-policy-44.3-1.fc44`, `selinux-policy-42.1.18-4.el10`), so
  this leaves an enforcing host — the RedHat-family default — unable to run the example stack.
