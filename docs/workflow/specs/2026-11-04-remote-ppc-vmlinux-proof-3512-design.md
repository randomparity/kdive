# Remote-authority ppc64le vmlinux proof (#3512)

## Problem

The remote staging implementation already exists (#3131), but its live carrier hardcodes
x86_64. The remote authority frame already validates the selected guest architecture against
the provider host and selects the matching registered root. Native execution also exposed the
bounded prerequisite and existing-provider corrections specified below.

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

## Failure model

- Actors and deployments: configured native x86_64/ppc64le remote authorities and the approved
  separate Linux host-process controller; native POWER/KVM-HV is required for this proof.
- Invariants and assets: uploaded SHA, real drgn, restoration and inventory/capacity criteria;
  exact revisions, identity/ownership, bounded private documents, captured original modules,
  30-second monotonic console idle and 300-second monotonic invocation caps remain mandatory.
- Accepted failures: an absent lane stays dormant in ordinary collection; unsupported/mismatched
  architecture fails before allocation. Missing matching root filesystem support (including
  CONFIG_BTRFS_FS for btrfs), modules, guest agent or drgn prevents proof. Unwritable console,
  individual blocking I/O exceeding the idle bound or work
  exceeding the invocation cap remain incomplete and use existing recovery/cleanup. Neither
  missing hardware, TCG, skip, failed staging nor offline evidence completes the native criterion.
- Covered elsewhere: local staging #3130, legacy delivery #3124, host readiness and ordinary
  recovery/cleanup contracts retain their existing owners and guards. No HMC/LPAR work.

## Global constraints

Python 3.14; targets x86_64 and ppc64le. Reuse the existing provider, appliance builder, closed
runtime, trusted console fields and artifact-upload contracts. No public API, schema, migration,
dependency version, timeout increase, background thread, guest shell or network capability.

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

### Producer compliance with the existing idle contract

A journal-clean isolated native replay measured completed source validation, source manifest,
copy and normalization, then the unchanged supervisor's 30-second idle expiry during destination
hashing, before fsync or depmod. A private numeric-stage image may affect timing and is diagnostic,
not candidate proof. Earlier offline rebinding before journal recovery contaminated diagnostic
scratch metadata; controlled offline A/B establishes that separate cause. Originals remain intact.

Extend only `deploy/remote_module_appliance/appliance.py`, its existing deployment tests and
appliance README, plus the existing remote runbook/spec/proof artifacts. Keep accepted ADR-0589's
policy and existing ownership; this implements producer compliance, not a new console policy.

After the validated operation is read, preserve the initial trusted `phase=accepted` announcement.
Enable one process-local monotonic throttle then: at completed hash/copy chunks (at most 1 MiB),
manifest entries, normalization steps and file/directory sync completions, emit the SAME fixed
`phase=accepted` line when at least five seconds have elapsed since the last announcement.
The interval is measured in seconds on the appliance's monotonic clock, scoped to that operation.
It limits emission frequency; a blocked syscall does not gain a heartbeat or immunity from idle
expiry. Missing console activity can still cause the existing host abort/recovery after 30 idle
seconds; reducing/repairing the input or host and normal retry remains the recovery path.

The repeated advisory means the accepted process is performing work or waiting for its live
bounded depmod child; it is neither durable phase advancement, byte-integrity proof nor install
success. Persisted result/checkpoints remain authoritative. Existing `_filter_console` accepts
this literal; change neither its whitelist nor the supervisor's idle/hard caps. No path, release,
identity, guest text or arbitrary child output enters the advisory.

Use a streamed copy function for `shutil.copytree(symlinks=True)` so one long regular file offers
chunk completion points. Preserve byte content, copy2-equivalent stat/xattr metadata and literal
symlink behavior; close files on failure and retain existing partial-stage recovery. Source remains
validated and read-only, destination is a newly owned tree; do not follow a substituted destination.

Run the same fixed depmod argv/environment with DEVNULL output via `Popen.wait` slices of at most
five seconds. While that child remains alive, a wait slice expiry may emit the same throttled
advisory as liveness, not completed work. Preserve one absolute 300-second monotonic depmod deadline
per child call; it does not restart per slice, and the host's earlier invocation cap still wins.
Kill and reap a live child on deadline, wait error or unexpected announcement failure. Preserve
stable DEPMOD_FAILURE behavior for spawn/nonzero/timeout errors and existing flush/recovery rules.

Threat model:
- Boundary inventory: no added/widened input or privilege boundary; existing validated module
  tree→copy, fixed child execution, trusted console→existing filter and image→loader boundaries.
- Actors: authenticated tenants supply bounded module trees; the isolated init owns its new
  destination and reads an immutable source. Operators pin the rebuilt kernel/runtime/image.
- Controls: preserve manifest size/count/path/symlink/metadata rules and copy2 metadata; fixed
  rate-bounded console literal remains filtered/redacted; fixed argv/env/DEVNULL child has no
  shell/network/output capture; existing builder/install/loader reverify immutable image identity.
- Out of scope: OS/storage faults and individual blocked I/O remain existing bounded failures,
  not proof success. Existing auth/tenancy and unfinished-preparation recovery guards remain.

Validation Mode: focused-test. Exercise actual main→hash/copy/checkpoint and depmod paths with
fake monotonic time/slow I/O and child boundaries, never mocking progress logic. Long single-file
work and copy metadata/symlinks must pass; original code fails the observed >30-second silence
case. Prove throttling, disabled-before-main behavior, constant filtered output, absolute child
expiry, nonzero/spawn failures and kill/reap including announcement exceptions. Controlled removal
of hash/copy callbacks must redden behavior tests. Existing supervisor idle/hard tests remain green.
Validation Mode: task-test-not-applicable for explanatory README/runbook/proof prose only.
Rebuild and verify the actual closed native image, then require separately authorized unchanged
native carrier SHA/drgn/restoration/cleanup proof at matching final revisions; no diagnostic counts.
