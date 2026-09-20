# Remote supplied ROOTFS live proof (#1516)

## Problem and authority

ADR-0440 implements worker-local supplied qcow2 staging but its real remote storage and
boot contract lacks a collected proof. Issue #1516 and its frozen WORK:SCOPE bind this
change to five proof arms and operator-volume preservation. No new architecture decision
is needed: the accepted provisioner and upload owners remain the execution path.

## Design

Add an opt-in carrier under `tests/live_vm/`, marked `live_vm` and `live_vm_remote`.
Its gate is triggered by `KDIVE_LIVE_VM_REMOTE_ROOTFS`: unset skips inside each test;
set requires a readable absolute local qcow2, verified `qemu+tls://` remote URI,
operator-staged comparison volume, and IP-literal GDB address. It consumes existing
remote variables without changing other carriers' gates. No S3, kernel/initrd upload,
or reconciler is involved in ADR-0440 provisioning, so those are not prerequisites.
Register the one new test variable and regenerate the config reference.

Use the production `RemoteLibvirtProvisioning` with its injected connection bundle,
opening the operator's verified TLS URI through `libvirt.open`. Each test generates
a fresh UUID, proves its domain and two deterministic volume names absent before
mutation, and uses the configured source's parent as the test-only allowed root.
The comparison base must not equal either generated volume name. Keep operator
base volume XML/path evidence before and after each arm.

The carrier proves these bounded behaviors:

1. Supplied source: provision using `LocalComponentRef`; observe actual upload invocation,
   real pool base volume, overlay XML backing path, running domain and responsive guest
   agent. Teardown through the production provisioner; require domain, overlay and base
   absent before fixture fallback cleanup.
2. Partial upload: exercise both an interrupted send after at least one real transmitted
   chunk and a finish fault after real stream transmission. Use a real libvirt stream;
   inject only the named boundary failure. Require infrastructure failure, injected-fault
   evidence and no per-System base volume before fallback cleanup.
3. Failed provision: allow real upload and overlay creation, then inject a libvirt
   define fault. Require provisioning failure and absent supplied base/overlay before
   fallback cleanup. Also exercise the operator-staged path under this fault and preserve
   its base.
4. Operator compatibility: provision without a source; reject any upload invocation,
   observe running guest and overlay backing the staged base, then teardown and prove
   the operator volume survives unchanged.
5. Format rejection: place a non-qcow2 file in the test's local temporary directory,
   invoke normal supplied provisioning with that allowed root, observe configuration
   error and zero remote volume-creation calls. Require both generated names absent.

Cleanup is invocation-scoped. Attempt production teardown and independent checks/removal
for the exact UUID domain and volume names; never enumerate-and-delete by prefix.
Preserve the primary assertion and report cleanup failures together. A failed production
cleanup assertion remains red even when fallback removes the scratch residue.
Independent pool observations use a fresh TLS connection so cached handles cannot stand
in for remote state. Each real host run records the exact tested checkout and arm counts.

## Alternatives

- Extend existing provider-op tests with one ROOTFS carrier (selected): retains the
  existing production seams and confines proof setup to this capability.
- Require the generic remote gate: adds unrelated S3, reconciler and external-boot
  prerequisites to a ROOTFS-only proof, without observing those capabilities.
- Drive the HTTP spine: covers more services but does not make precise injected
  upload-boundary failures simpler; that independent transport scope is excluded.

## Global Constraints

Python 3.14 managed with uv; no new dependency or toolchain floor. Declared project
targets are x86_64 and ppc64le; this carrier exercises the currently supported x86_64
remote disk-image profile and does not claim a ppc64le live run. Production changes
require an actual live reproduction of a #1516 root cause. Operator-owned base-image
volumes are never mutated or deleted. Public evidence contains no private host identity.

## Failure model

- Actors and deployments: an operator running the explicit carrier against a disposable
  x86_64 KVM remote libvirt host with verified mutual TLS and a guest-agent-ready image.
- Invariants and assets at stake: preserve the operator base; only generated invocation
  artifacts are removable; red proof cannot become green through fallback cleanup;
  supplied bytes reach the real pool and the overlay actually boots against them.
- Accepted failure classes: force-killed test processes can leave exact scratch artifacts
  for operator recovery (no crash-reaper proof requested); permanent service loss makes
  cleanup fail loudly and requires operator recovery; image-content failures remain the
  supplier's responsibility under ADR-0440 and do not count as successful proof.
- Covered elsewhere: new sources and upload semantics (#1433 follow-up); general remote
  framework (#1424); other parity capabilities (#1423 children); permanent remote CI
  (deployment/runner workflow); unrelated production refactors (separate issues).

## Threat model

- Boundary inventory: new test environment input controls existing TLS transport,
  local file reads and scratch-volume writes; no product entry point is added or widened.
- Actor model: trusted operator supplies the source and verified target; remote service
  and local image are fallible. This is not a multi-tenant service entry point.
- Control per boundary: gate requires verified TLS and bounded expected companion forms;
  normal local-source containment and magic validation execute; generated UUID names and
  absence checks confine cleanup; XML uses existing production renderers; diagnostics
  stay private until summarized and scanned for public posting.
- Out of scope: hostile privileged operator or remote administrator; standing credential
  lifecycle (existing TLS deployment); new source kinds and unrelated capture semantics.

## Success and validation

All five named arms pass on an operator-provided disposable remote host, with two upload
fault variants and both supplied/operator failed-provision variants. Each success is a
real pool/domain observation; mocked helper tests only establish gate and cleanup behavior.
Gate tests prove absent skip and configured-invalid failure before transport. Run scoped
carrier tests, `just lint`, `just type`, generated-reference checks, staged hooks and final
`just ci`. Record live arm counts separately from the ordinary suite and CI; no skip counts
as live acceptance. The runbook documents inputs, exact invocation, fault boundaries,
scratch cleanup and evidence limitations.
