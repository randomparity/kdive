# SUSE rootfs family and openSUSE catalog images (#825)

## Authority and status

Issue #825 and its parent epic #822 authorize the SUSE-family slice of the local-libvirt rootfs
catalog. The frozen scope is the latest complete `WORK:SCOPE` comment carrying token
`q825-eb2c81ec`. The operator approved the quest and its frozen scope on 2026-09-22; this revision
also records the boot and readiness corrections found while proving that approved scope live.

[ADR-0251](../../adr/0251-local-multidistro-rootfs-catalog.md) governs the catalog,
`FamilyCustomizer`, customization-boot, and incomplete-core disclosure contracts. This design is
an extension through those seams and does not require a new architecture decision.

## Problem

The catalog parser accepts `family = "suse"`, but the family registry has no implementation and
the catalog has no openSUSE rows. Resolving a SUSE entry therefore fails before a rootfs can be
built.

The package assumptions in issue #825 are no longer true in current official repositories:

- the selected Tumbleweed repository supplies makedumpfile 1.7.7, whose packaged changelog limits
  x86_64 support to Linux v6.14, and drgn 0.1.0;
- Leap 15.6 supplies makedumpfile 1.7.4 and no drgn package.

Neither distribution can truthfully claim filtered kdump capture for KDIVE's v7.0 test kernel.
Leap also cannot advertise the build-fact `drgn` capability. The approved contract keeps both
images distribution-native, reports both as kdump-incapable for v7.0, and advertises drgn only on
Tumbleweed.

SUSE's kdump implementation creates the final `vmcore` pathname before makedumpfile finishes and
retains that partial file on failure. It then reboots. KDIVE's existing incomplete-core contract
recognizes `vmcore-incomplete` and waits for a powered-off domain before harvesting. A stock SUSE
guest would therefore present a partial core as complete and would force the host onto its capture
timeout rather than the intended remediation.

## Scope

Add a debug-only `SuseFamily` and these x86_64 catalog identities:

- `opensuse-tumbleweed-kdive-ready` from a dated official Tumbleweed Minimal VM Cloud qcow2;
- `opensuse-leap-kdive-ready-15.6` from a versioned official Leap 15.6 Minimal VM Cloud qcow2.

Both bases are pinned by URL and SHA-256. Package installation remains unpinned, matching the
existing catalog contract: zypper resolves the selected distribution's current official packages,
and the build records the installed versions as provenance.

The change includes family behavior, catalog evidence, focused tests, two live lifecycle proofs,
and current support/lifecycle documentation. It does not add SLES, ppc64le, SUSE build-host
images, third-party repositories, source-built guest tools, another provider, hosted live CI, or a
new kdump capability model.

## Approaches considered

### Dedicated family adapter — selected

Implement `SuseFamily` at the existing family seam. Keep zypper, SUSE kdump configuration, and
distribution-specific package divergence inside that adapter. Add a small guest-side post-capture
adapter so SUSE produces the filenames and power state the existing local-libvirt harvester
already understands.

This keeps the change bounded to the extension seam ADR-0251 created and leaves the RHEL and
Debian behaviors intact.

### Generalized declarative family policy

Move package-manager commands, service names, kdump configuration, MAC posture, and marker rules
into a shared policy object, then express all three families as data. The current family classes
contain behavior that does not reduce cleanly to one table, including EL-major package rules,
Debian's `USE_KDUMP`, and SUSE's post-capture bridge. Generalizing them would broaden issue #825
and replace working code without reducing the SUSE correctness burden.

### Source-built guest tooling

Build makedumpfile 1.7.9 for Tumbleweed and drgn for Leap 15.6. That would introduce guest
toolchain, source provenance, update, and supply-chain contracts not present in the catalog today.
The operator explicitly selected official distribution packages only, so this approach is
excluded.

## Design

### Family registration and supported kind

`src/kdive/images/families/suse.py` implements `FamilyCustomizer` and is registered under
`"suse"` in `src/kdive/images/families/__init__.py`.

The family accepts only `kind = "debug"`. A SUSE build-host row fails with a categorized
configuration error rather than receiving an empty, partial, or guessed toolchain. The generic
registry evidence test enumerates the supported family/distro/kind cases explicitly, so this
intentional restriction does not weaken checks for RHEL or Debian build images.

The two distro identities are `opensuse-tumbleweed` and `opensuse-leap`. The former selects the
drgn-bearing package set; the latter selects the package set without drgn. Unknown SUSE distro
identities fail as configuration errors rather than silently receiving one release's packages.

### Packages and capabilities

The common debug package set is:

- `kdump`, `kexec-tools`, `makedumpfile`, and `dracut` for capture;
- `crash` for postmortem tooling;
- `openssh-server` for the local-libvirt SSH seam.

Tumbleweed additionally installs `drgn`. Leap 15.6 does not. zypper refreshes repository metadata
before a non-interactive, no-recommends install.

Both images declare `ssh`, `apparmor`, and `kdump`. Tumbleweed also declares `drgn`. The
capabilities remain build facts; `images.describe` computes kernel-relative kdump and live-drgn
signals from build-recorded provenance as it does for existing families.

### Ordered customization

The SUSE customizer emits these steps in order:

1. refresh zypper metadata and install the selected packages;
2. enable `sshd.service` and `kdump.service`;
3. write the existing NMI-panic sysctl used by local `control.force_crash`;
4. stage and configure the SUSE kdump post-capture adapter described below;
5. apply the shared cloud-init/NoCloud first-boot steps;
6. stage the drgn helper and drgn-version marker only when drgn is installed;
7. write the makedumpfile-version marker on both images;
8. upload and enable the shared `kdive-ready.service` ordered after `kdump.service`.

Normalization rewrites `/etc/fstab` to the whole-disk ext4 layout and removes `/etc/crypttab`, as
the Debian AppArmor path does. It does not touch SELinux configuration or request an autorelabel.
The implementation stays local to `SuseFamily`; extracting the two small guestfish operations from
the existing Debian family would change unrelated code without adding a third behavior.

SUSE names its baseline initrd `/boot/initrd-<kernel-version>`, so the shared kernel selector
accepts that spelling in addition to the existing `initramfs-<kernel-version>.img` and
`initrd.img-<kernel-version>` forms. Tumbleweed's Minimal VM initrd also contains KIWI's
`20-kiwi-repart-disk.sh` pre-mount hook. That hook assumes the source appliance's partitioned disk
layout and powers off after waiting for a partition that cannot exist once KDIVE repacks the image
as whole-disk ext4. During SUSE normalization, a bounded streaming zstd/newc transform removes only
that exact hook pathname. The transform refuses an initrd whose expanded archive exceeds 2 GiB,
replaces the file atomically, and leaves an archive without the hook byte-for-byte unchanged.

Both SUSE rows use `eth0` as the cloud-init network stanza identifier, retaining the wildcard MAC
match used by the other families. That predictable identifier lets cloud-init generate a usable
NetworkManager or Wicked connection for QEMU's NIC instead of binding the connection to the
arbitrary YAML key. This keeps cloud-init as the uniform first-boot network owner required by
ADR-0288; there is no family-specific interface file.

The shared readiness unit remains ordered after `network-online.target`, which both SUSE images
back with their native wait-online service. Baseline provisioning does not consume this serial
marker: ADR-0272 deliberately defines the System's `ready` state as successful domain start, and
ADR-0294 proves eventual guest reachability through the product's bounded
`systems.authorize_ssh_key` path. Live testing rejected a SUSE-only readiness drop-in and a raw
banner assertion because neither can change the baseline provisioning contract and the latter
duplicates a client path ADR-0294 explicitly rejected. It also exposed a shared probe defect:
QEMU accepts a host-forward TCP connection before the guest sshd exists, while the probe held that
banner-less connection for its whole flow deadline. The probe now caps each banner read at two
seconds and reconnects within the deadline. A non-SSH banner still fails immediately, and the
terminal verdict distinguishes a never-accepted TCP connection from an accepted connection that
never produced an SSH banner. [ADR-0672](../../adr/0672-authorize-ssh-has-a-longer-preflight-window.md)
keeps the viewer probe at 15 seconds but gives authorization 30 seconds after Leap live evidence
showed sshd starting immediately after the shorter window expired.

### SUSE kdump compatibility adapter

The image stages `/usr/local/sbin/kdive-suse-kdump-post` as a fixed, non-user-controlled script
and adds it to `KDUMP_REQUIRED_PROGRAMS` in `/etc/sysconfig/kdump`. `KDUMP_POSTSCRIPT` invokes it
without arguments. That single command is valid under both supported kdump implementations: Leap
15.6 executes the configured value directly, while Tumbleweed evaluates it as a shell command.
The adapter searches only the fixed SUSE file-target root `/kdump/mnt/var/crash`.

SUSE's save script decides that a vmcore succeeded from makedumpfile's exit status and writes only
that result to `README.txt`. Makedumpfile 1.7.4 and 1.7.7 return success for a v7.0 dump while also
warning that the kernel is unsupported and the result may be incomplete. Those warnings go to the
crash console, not the README. The image therefore appends one static redirection to SUSE's
supported `MAKEDUMPFILE_OPTIONS`: vmcore-conversion stderr is retained at
`/tmp/kdive-makedumpfile-stderr` inside the disposable capture initramfs. The postscript reads that
fixed file and treats either exact makedumpfile warning as an incomplete result. No vendor script
is patched and no version-to-kernel compatibility table is duplicated in the guest.

After SUSE has written its final `README.txt`, the adapter:

1. enumerates direct child directories containing `vmcore`;
2. keeps the complete name only when exactly one such directory exists and its `README.txt`
   contains the exact record `vmcore status: saved successfully`, and neither the README nor the
   captured makedumpfile stderr contains an exact unsupported/incomplete warning;
3. otherwise renames every direct-child `vmcore` to `vmcore-incomplete`;
4. synchronizes filesystems, unmounts them, and forces the capture guest off.

A successful, warning-free vmcore keeps its final name. A failed, truncated, or explicitly
unsupported vmcore is never left under the complete name. Zero candidates is a no-op for the core names and the existing no-core path applies;
multiple candidates fail closed by losing every complete name. Each KDIVE System starts from a
fresh per-System overlay and `force_crash` moves it into the terminal `CRASHED` state, so the
ordinary lifecycle has one current capture directory. The multiple-candidate behavior remains a
defensive guard against unexpected or stale layouts. The fixed script receives no tenant data,
does not transfer the core, and changes no host-side retrieval code.

The poweroff is part of the adapter because SUSE's default immediate action is reboot while the
local harvester's settled signal is domain shutdown. The live proof must establish that the
capture initramfs contains the adapter and every external command it calls, that the rename occurs
on the unsupported v7.0 kernel, and that the domain reaches shutoff before `vmcore.fetch` starts.
The latter is polled directly through the worker's published libvirt URI, so the host harvester's
120-second timeout cannot create the asserted power state.

### Catalog evidence for an absent tool

TOML has no null scalar and the existing catalog requires a nonempty `drgn_version`. Introduce one
explicit value, `drgn_version = "absent"`, which the loader maps to `None` in
`RootfsCatalogEntry.drgn_version: str | None`. Omitting the field remains a configuration error;
the sentinel records a reviewed absence rather than allowing accidental omission.

Existing version strings and their behavior do not change. Focused catalog tests require the Leap
row to parse to `None`, lack the `drgn` capability, and compute live-drgn as `not_applicable`
because the tooling is absent. Tumbleweed records the verified repository version and retains the
capability.
The built image's marker probe remains authoritative provenance: Leap emits no drgn marker and its
published provenance omits `drgn_version`.

### Catalog rows and documentation

Each row records the selected official image's dated URL, SHA-256, distro identity, release or
snapshot version, `family = "suse"`, `arch = "x86_64"`, `kind = "debug"`, and curated tool
evidence. The Tumbleweed row pins a dated snapshot rather than a rotating alias. The Leap row pins
the versioned build path rather than a latest alias.

`docs/operating/platform-support.md` adds the two x86_64 combinations and states that both are
incapable for a v7.0 target with their distribution-native makedumpfile versions. The image
lifecycle runbook names the two `build-fs --image` values and the per-image live-proof environment
variables without claiming hosted or ppc64le coverage.

### Live proof

The live-stack suite receives distinct inputs for the two artifacts:

- `KDIVE_GUEST_IMAGE_SUSE_TUMBLEWEED`;
- `KDIVE_GUEST_IMAGE_SUSE_LEAP_15_6`.

The existing per-family SSH proof is generalized to named image cases and covers both SUSE rows
independently. After the System reaches `ready`, it invokes the terminal
`systems.authorize_ssh_key` job. Its bounded product-path retry is the existing ADR-0294 contract,
with ADR-0672's 30-second authorization preflight:
a succeeded drain proves the NIC leased, the forward bridged, sshd answered, and authenticated SSH
worked without inventing a stronger provision-readiness meaning. The reconnecting banner probe is
load-bearing for SUSE's longer baseline boot. A SUSE incomplete-capture proof is parameterized over
the same two inputs and uses the ordinary HTTP lifecycle:

1. allocate and provision the selected image;
2. create a Run, require `KDIVE_KERNEL_SRC` to report `kernelversion = 7.0.0`, and require the
   actual `arch/x86/boot/bzImage` header release to equal the tree's `kernelrelease`; then build
   and upload that artifact, install it with a release-specific command-line proof token, and boot
   it;
3. inspect the running domain XML to require the per-Run staged kernel path and proof token;
4. force-crash under the existing destructive-operation gate;
5. poll the named libvirt domain to shutoff through the worker's published URI, with a deadline
   below the harvester's 120-second fallback, before requesting capture;
6. request `vmcore.fetch` and wait for the terminal capture job;
7. require `readiness_failure`, `failure_detail_reason = "kdump_core_incomplete"`, and the existing
   recovery text naming `method="host_dump"`;
8. release the allocation in a failure-safe `finally` block.

The test does not accept a generic capture failure, an empty `/var/crash`, a successful partial
core, or a host-side timeout as proof. A live run records the exact built commit and artifact paths
for both parameters; private machine identifiers stay out of public artifacts.

## Failure model

- Actors and deployments: an operator builds either approved x86_64 catalog row and runs it under
  the local-libvirt customization and lifecycle stack on a KVM-capable development system.
- Protected invariants: catalog sources stay SHA-256 pinned; capability tags describe installed
  tooling; published provenance comes from the built image; a partial core is never exposed as a
  complete vmcore; the capture guest reaches a harvestable terminal power state; existing RHEL and
  Debian behavior does not change.
- Accepted failures: official repository or image availability can fail a build loudly; rolling
  Tumbleweed package versions may move after the curated snapshot and are disclosed by actual
  build provenance; neither row supports filtered v7.0 capture with the approved package set.
- Required handling: unknown SUSE distros and SUSE build images fail as configuration errors;
  absent Leap drgn is explicit; package/configuration failures fail customization; missing or
  malformed capture output remains a readiness failure rather than a successful artifact.
- Covered elsewhere: image download retry policy, generic job retry/cancellation, `host_dump`
  implementation, provider authorization, object-store publication, and kdump/drgn capability
  algorithms retain their existing owners.
- Excluded deployments: SLES, SUSE ppc64le/TCG, remote-libvirt, cloud, bare metal, hosted live CI,
  and SUSE build-host images.

## Threat model

The new external inputs are two official HTTPS image URLs and their checked SHA-256 values. The
existing cloud-image acquisition path enforces the digest before customization. Package metadata
and RPMs come only from the images' official configured repositories.

The post-capture script and sysconfig values are repository constants. They contain no user input,
secret, network destination, or artifact payload. The script takes no arguments, searches only
direct children of a fixed root, checks a fixed status string, renames only the two fixed filenames
beneath that root, and terminates the disposable crash kernel. Focused tests inspect the rendered
command and script; the live proof establishes the initramfs execution path.

The initrd normalizer reads only the selected local image's trusted baseline initrd, rejects
malformed newc records and unsupported compression, enforces the 2 GiB expanded-size limit before
writing each chunk, and removes one fixed pathname. It does not interpret archive filenames as
host paths or extract entries onto the host filesystem.

No authentication, authorization, tenant boundary, persisted schema, secret handling, or host
command boundary changes.

## Success criteria

1. `family_for("suse")` returns the new customizer; unsupported SUSE identities and build kind
   fail with categorized configuration errors.
2. Focused tests prove zypper ordering, package divergence, capability evidence, service and
   sysconfig configuration, marker/helper coupling, AppArmor normalization, baseline-initrd
   selection, bounded KIWI-hook removal, network configuration, shared readiness upload, and
   reconnecting a QEMU forward that accepts before sshd answers.
3. The catalog loads both pinned rows, maps Leap's explicit absent drgn evidence to `None`, and
   computes both rows kdump-incapable for v7.0 without changing existing rows.
4. Both qcow2 images build through the real customization boot and record installed packages,
   makedumpfile, OS identity, and conditional drgn provenance.
5. Both images provision and answer SSH through the live HTTP stack.
6. Both images boot the current v7.0 kernel and return the exact existing incomplete-core
   remediation after force-crash without exposing a partial core as successful.
7. Current platform-support and image-lifecycle documentation names the installable rows and their
   limitations.
8. Focused guardrails and `just ci` pass; no excluded provider, architecture, image kind, package
   source, or capability redesign lands.

## Validation

- **Family contract — Mode: focused-test.** Run `just test-verbose
  tests/images/families/test_suse.py tests/images/families/test_capability_evidence.py`.
- **Catalog contract — Mode: focused-test.** Run `just test-verbose
  tests/images/test_rootfs_catalog.py tests/images/test_catalog_resolver.py`.
- **Build-plane compatibility — Mode: focused-test.** Run `just test-verbose
  tests/providers/local_libvirt/test_rootfs_build.py`.
- **Fast repository guardrails — Mode: guardrail.** Run `just lint`, `just type`, and
  `just test-changed` during implementation.
- **Image builds — Mode: live-test.** Build each named catalog row on an x86_64 KVM system and
  inspect its emitted provenance before using the artifact.
- **Lifecycle and remediation — Mode: live-test.** Start the host live stack from the exact source
  head, set both SUSE image variables, and run the two SUSE SSH and incomplete-capture parameters.
- **Pre-push parity — Mode: guardrail.** Run `just ci > <log> 2>&1 < /dev/null` as the final local
  gate, preserving the recipe's exit status.
