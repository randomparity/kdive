# Remote-authority ppc64le vmlinux proof (#3512)

## Problem

The remote staging implementation already exists (#3131), but its live carrier hardcodes
x86_64. The remote authority frame already validates the selected guest architecture against
the provider host and selects the matching registered root. No product staging code needs changing.

## Scope

Authority: issue #3512 and its `WORK:SCOPE` token `q3512-c782dd94`, approved 2026-11-04.
Retain x86_64 and add a repeatable ppc64le remote-authority arm. Local staging remains #3130;
legacy remote delivery remains #3124. Other parent criteria remain unchanged.

Read the configured authority host architecture with the existing `select_remote_lane` and
`remote_authority_inventory` boundaries; accept x86_64 and ppc64le, feeding both Cell fields.
The existing frame rechecks host architecture before allocation. Architecture-qualified cell
IDs keep evidence distinct. The unfiltered x86 invocation stays compatible. Retain the existing
upload, guest SHA-256, real default-search drgn, release, baseline boot, restored absence and
frame cleanup assertions. Observed architecture avoids a duplicate lifecycle or configuration key.

### Native bundle on a foreign controller

The approved deployment uses an ARM64 controller and a native POWER authority. The remote
activation helper requests an initrd but currently defaults its build profile to x86_64.
Pass the carrier closure's already observed architecture through `_prove` and an optional
`guest_arch` argument on `authority_faults.activate` to `start_external_boot_activation`;
retain x86_64 defaults for existing callers and add no repeated SSH inventory read. Extend
`tests/integration/live_stack/spine.py` and focused tests to reuse `KDIVE_PPC64LE_BUNDLE`.
When set for ppc64le, upload its `kernel.tar.gz` and `initrd.img` rather than running native
`strip`, `make modules_install`, or dracut on the controller. The native producer runs the
existing combined-tar and initrd helpers against the same complete kernel tree. The controller
still supplies that tree's `.config`, unstripped `vmlinux`, and `include/config/kernel.release`.
Before any upload declaration, require both nonempty files, gzip initrd magic, a regular
`boot/vmlinuz` with the same GNU build ID as tree `vmlinux`, and a matching module release.
Malformed archives and missing or mismatched inputs fail with the upload phase. Existing
server artifact/architecture validation and `runs.complete_build` remain authoritative.
An unset bundle retains the generated lane's native-initrd architecture guard.
No new API, schema, dependency, manifest format, identity shim, or foreign dracut path is added.
This necessary surface extension was approved by the campaign root on 2026-11-04 under the
same scope and exclusions; native SHA, drgn and restoration proof remains mandatory.

### Failure model

- Deployment: configured remote authority on native x86_64 or ppc64le; native POWER/KVM-HV
  is required for the new proof. TCG results cannot complete #3512.
- Missing lane remains dormant in ordinary collection. An unsupported observed architecture
  fails before entering the frame; architecture drift fails its existing precondition before
  allocation. No inventory mutation precedes those checks.
- Failed activation retains existing release/frame cleanup. Product failures are evidence to
  investigate and report, never grounds to loosen assertions or count skips as success.
- Missing matching filesystem, modules, guest agent, or drgn prevents proof. The uploaded
  kernel must support the actual root filesystem, including CONFIG_BTRFS_FS for a btrfs root.
- Existing deployment revision checks and root provenance checks remain authoritative.

## Success

1. Both supported authority architectures route to the matching existing frame Cell.
2. A native ppc64le remote run proves the uploaded SHA-256, drgn init_uts_ns release, restore
   removal, and unchanged authority domains/volumes/capacity.
3. The runbook gives an exact arm selector and prerequisites; a dated proof record identifies
   tested revision, native accelerator, kernel/config provenance, observations and cleanup.
   Hardware unavailable or a failed run is recorded as incomplete; the issue stays open.

## Validation

- Focused boundary test supplies observed x86_64/ppc64le through the inventory boundary,
  intercepts its frame, and verifies matching native Cells; unsupported architecture must fail
  before entering the frame. Red: current carrier still routes ppc64le as x86_64.
  Green: focused Linux pytest file.
- Native live execution is the functional proof,
  with deployed revision equal to the test candidate and a matching complete kernel tree.
- Runbook/spec/proof prose: task-test-not-applicable; no executable consumer validates prose.
  Run the repository doc guards. Never add tests that snapshot wording.

- Prebuilt upload focused tests: valid matching bundle uploads kernel/config/vmlinux/initrd,
  retains SHA declarations and completes with build ID without invoking native builders.
  Missing, malformed and mismatched bundle inputs fail before upload declaration; an unset
  bundle on a foreign controller still fails the existing generated-initrd guard. RED is the
  current unconditional architecture rejection; GREEN is the scoped Linux spine test file.
- Exercise the real remote inventory → carrier → activation → upload chain with SSH/MCP
  boundaries mocked, preserving the actual activation helper; ppc64le must reach both the
  Run build profile and upload selection. Existing local and x86 cases retain defaults.

### Controller source-build prerequisites

The ARM64 controller's live-group drgn installation builds from source and failed at
`autoreconf`. Extend the existing setup dependency check and generic `local_worker_host`
provisioning role to declare autoconf, automake and libtool for this supported source-build
case. Correct the existing POWER-only wording/condition; do not add a dependency framework.
Use the existing package names autoconf, automake and libtool through an Ansible package task
for aarch64/ppc64le; keep Rust requirements separate. The setup-check CLI regression and existing
worker-role structural runner must reject a missing prerequisite or incorrect architecture gate.
Document this source-build prerequisite in the existing installation guide.

The normal backend bucket-init one-shot builds the root Dockerfile even on this host-process
deployment. Its isolated ARM image needs the same drgn build tools/ELF headers and matching
libkdumpfile runtime library through the existing architecture condition. Preserve amd64
packages and pinned dependency versions. Verify the actual native ARM image build and normal
backend bucket initialization before live execution; host venv success is insufficient.

### Native image prerequisites

The required native image operation exposed the Debian guest-image prerequisite's assumption
that the running kernel is named `vmlinuz-<release>`; the POWER host instead installs the
regular `vmlinux-<release>` file. In `guest_image_prereqs/tasks/main.yml`, select those exact
running-release regular files in that order, fail actionably if neither exists, and make only
the selected kernel readable. Preserve the existing x86 preference. Extend the existing real
Ansible role runner with isolated x86, POWER, both and missing-file cases; demonstrate the
POWER case fails before the fix and rerun the actual image operation afterward.

The selected privileged direct-backend image workflow has demonstrated network access with
the host's AppArmor/userns policy unchanged. Tag only the three existing passt policy-relaxation
tasks so this deployment can explicitly skip them. Preserve default behavior and test that
selection excludes exactly those tasks while retaining kernel and dependency prerequisites.
Record the explicit selection in the runbook; this does not fix the existing blanket workaround
for other deployments. Include these permission boundaries in the final security review.

The actual controller `stage-volume` operation also requires `virt-inspector`; the generic
Fedora worker preparation installed only its Python binding. Reuse the existing distro
`guestfs-tools` package in `local_worker_host/tasks/packages_redhat.yml`, gated to Fedora,
with a package/condition guard in the existing role runner. Retain the original failed command,
verify the distro's actual executable provider, and rerun staging with its unchanged mandatory
inspection and upload validation. No native ARM guest provider is introduced.

### Native provider feature block

The first real carrier reached provisioning and libvirt rejected pseries ACPI before guest boot.
The remote renderer emits ACPI/vmcoreinfo unconditionally, while the accepted shared architecture
trait already gates that block in local rendering. In `remote_libvirt/lifecycle/xml.py`, reuse
`arch_traits(profile.arch).emit_acpi_features` for this existing block; preserve x86 bytes/order
and all other features. Extend the existing remote provisioning and System-authority tests to
require POWER feature absence, x86 preservation, successful canonical identity/replay and refusal
of unexpected identity changes. Do not rewrite fixture XML or bypass authority validation.
Retain the failed native journal and cleanup evidence. Demonstrate renderer RED before fixing,
then focused Linux GREEN and a real native define/readback/undefine using the product renderer
before redeploying both runtime ends and rerunning the unchanged full carrier.

### Documented pSeries panic default

The real product-rendered define now succeeds, but canonical readback rejects libvirt's implicit
`<panic model="pseries"/>`. The [libvirt panic-device contract](https://libvirt.org/formatdomain.html#panic-device)
documents this always-enabled pSeries firmware notifier and forbids an address for that model.
Extend only the existing harmless-default normalization in `remote_libvirt/system_authority.py`:
on ppc64le with a pSeries machine, absent or one exact empty leaf with only `model="pseries"`
projects as omitted. Duplicate devices, other models, extra attributes, children, content text
or non-whitespace tail, and panic on x86 or another machine remain unexpected and fail identity.
Do not broadly allowlist panic, change other identity checks, add unconditional projection keys,
or change existing x86 v2 digests. Retain a pre-fix x86 digest vector and verify it is unchanged.
Update the existing System-authority fake boundary with this measured libvirt default; test
matching canonical provision/replay and all malformed/wrong-architecture variants on both live
and inactive XML. Native define/readback/undefine must prove canonical equality and unchanged
inventory before the unchanged full carrier rerun. If digest preservation is impossible, stop
for an explicit persistence/version decision; no silent projection-version change is authorized.

### Logical scratch capacity and physical allocation

The native appliance produced a validated installed result, but its reader rejected the
10 GiB scratch volume because physical allocation exceeded logical capacity by 4 KiB.
The [libvirt volume contract](https://libvirt.org/formatstorage.html#storage-volume-general-metadata)
permits allocation above capacity for metadata overhead. In the existing remote module result
reader, require the exact configured logical capacity and nonnegative integer allocation without
comparing physical allocation to logical capacity. Reject malformed types rather than coercing
them. Preserve ownership checks, logical sparse data/hole bounds, fixed download length,
document limits, deadlines and cleanup; no tolerance, caller limit or schema is introduced.
Test real extraction with the observed extra allocation, wrong capacity, negative/malformed
allocation and stream overflow. Retain native result/volume metadata and rerun the same reader
on the inactive owned volume before full carrier proof. Earlier appliance timeouts remain
separate inconclusive failures; an offline installed file is diagnostic evidence, not live proof.
