# SUSE rootfs family and openSUSE catalog images (#825)

## Authority and status

Issue #825 and its parent epic #822 authorize the SUSE-family slice of the local-libvirt rootfs
catalog. The frozen scope is the latest complete `WORK:SCOPE` comment carrying token
`q825-eb2c81ec`. The operator approved this design on 2026-09-22.

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

### SUSE kdump compatibility adapter

The image stages `/usr/local/sbin/kdive-suse-kdump-post` as a fixed, non-user-controlled script
and adds it to `KDUMP_REQUIRED_PROGRAMS` in `/etc/sysconfig/kdump`. `KDUMP_POSTSCRIPT` invokes it
without arguments. That single command is valid under both supported kdump implementations: Leap
15.6 executes the configured value directly, while Tumbleweed evaluates it as a shell command.
The adapter searches only the fixed SUSE file-target root `/kdump/mnt/var/crash`.

After SUSE has written its final `README.txt`, the adapter:

1. enumerates direct child directories containing `vmcore`;
2. keeps the complete name only when exactly one such directory exists and its `README.txt`
   contains the exact record `vmcore status: saved successfully`;
3. otherwise renames every direct-child `vmcore` to `vmcore-incomplete`;
4. synchronizes filesystems, unmounts them, and forces the capture guest off.

A successful vmcore keeps its final name. A failed or truncated vmcore is never left under the
complete name. Zero candidates is a no-op for the core names and the existing no-core path applies;
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
row to parse to `None`, lack the `drgn` capability, and compute live-drgn as incapable because the
tooling is absent. Tumbleweed records the verified repository version and retains the capability.
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
independently. A SUSE incomplete-capture proof is parameterized over the same two inputs and uses
the ordinary HTTP lifecycle:

1. allocate and provision the selected image;
2. create a Run, build and upload the current v7.0 kernel, install it, and boot it;
3. force-crash under the existing destructive-operation gate;
4. poll the named libvirt domain to shutoff through the worker's published URI, with a deadline
   below the harvester's 120-second fallback, before requesting capture;
5. request `vmcore.fetch` and wait for the terminal capture job;
6. require `readiness_failure`, `failure_detail_reason = "kdump_core_incomplete"`, and the existing
   recovery text naming `method="host_dump"`;
7. release the allocation in a failure-safe `finally` block.

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

No authentication, authorization, tenant boundary, persisted schema, secret handling, or host
command boundary changes.

## Success criteria

1. `family_for("suse")` returns the new customizer; unsupported SUSE identities and build kind
   fail with categorized configuration errors.
2. Focused tests prove zypper ordering, package divergence, capability evidence, service and
   sysconfig configuration, marker/helper coupling, AppArmor normalization, and readiness order.
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
