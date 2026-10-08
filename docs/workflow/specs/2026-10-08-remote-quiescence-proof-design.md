# Remote capture quiescence ordering proof

## Authority and problem

Issue #2816 requires the skipped separate-provider ordering carrier to execute.
The campaign-approved slice is Resource-bound remote operation quiescence plus
failure-owner remapping. Scope comment6055230965, token q2816-e8a90c37, campaign
73d4edacff-8cf13f64-8877-4610-beaf-496193501043 records the eight-field charter.
Human2026-10-07 approved the exact scopes and exclusions. ADR-0558 already requires
accepted monitor mutation, terminated client and fresh independent observation.
No runtime architecture, public interface, permission or secret policy changes.

The current carrier always calls pytest.skip after remote preflight. The local
live traffic-capture test supplies a proven native mechanism: blockdev-add waits
for an accepted NBD handshake while a new monitor connection waits behind it.
The remote production builder reconstructs TLS from RemoteCaptureConfiguration,
including Resource UUID, URI and secret references; its probe refuses a mismatched
Resource and performs correlated detach/query on a fresh connection.

## Choice

Reuse the native blocked-NBD mechanism on the separate provider instead of adding
a FIFO file-operation framework. A fixed test-only Python listener on provider
loopback accepts the QEMU connection and withholds the handshake. TCP loopback
avoids shared-filesystem assumptions and creating a socket in another security
label; no externally reachable listener, firewall or SELinux change is proposed.
The accepted connection proves QEMU actually entered the operation. No fake
monitor, sleep-only scheduling assumption or successful envelope stands in.

Rejected alternatives: a local listener would not prove separate-host ordering;
client submission alone is not acceptance; literal FIFO adds remote filesystem
permissions without strengthening the barrier; arbitrary operator-supplied
commands create an unnecessary interface. Reuse existing REMOTE_PROVIDER_SSH
and Resource inventory/TLS selection. The listener implements only fixed
ready/accepted/release/closed messages, never an arbitrary command evaluator.

## Surface and ownership

- Replace tests/integration/test_remote_capture_operation_quiescence_live.py's
  unconditional skip with its actual live invocation, retaining its existing
  test node and live_vm/live_vm_remote selection.
- tests/integration/live_stack/remote_quiescence.py owns the bounded fixture,
  production TLS probe assembly and evidence orchestration. Its owning ordinary
  test module exercises failure/ordering/cleanup logic with external boundaries
  replaced; no collected test-module imports.
- scripts/coverage_campaign/contract.py and obligations.toml retain original
  failure-resource IDs/assertions and remap x86 orchestration cells to #3117,
  x86 resource limits to #3118 and POWER cells to #2818. Add the missing exact
  remote-quiescence scenario binding required by this carrier, with x86 owned by
  #2816 and POWER pending #2818. No generic outcome/schema extension.
- Owning contract tests and remote-live-stack/live-testing runbooks record the
  new executable binding, actual prerequisites, bounded behavior and limitations.
  This specification is durable; the plan is private.

## Resource-bound real System

Reuse on_remote_system with the established Enterprise Linux base volume and
actual server/worker/reconciler stack. Observe separate provider OS/architecture,
KVM domain type, image hash and owned overlay through existing observer support.
Before mutation, read the minted System's Allocation/Resource identity and name
from existing public data or a read-only repository query. Resolve that exact
name through remote_config_for_resource, not a first-host global URI. Require
its URI to match the selected remote contract and verify TLS and SSH observer
see the same newly minted domain UUID. A mismatch fails before QMP or listener.

Serialize capture_operation_configuration(resource_id, resource_name), parse the
existing RemoteCaptureConfiguration and call build_capture_quiescence unchanged.
The mutator opens its own production TLS connection from the same references;
the absence probe creates a separate fresh TLS connection. References and raw
configuration stay private. Prove a wrong Resource UUID fails before connection
in a focused test; real proof uses the actual allocated Resource, not uuid4.

No external authority is installed or claimed. No kernel build is needed for an
ordering experiment on QEMU: the existing base image supplies the booted guest,
and image identity is recorded. This is not kernel-corpus, capture or debugger
qualification. Missing base-image or stack prerequisites block the proof.

## Ordered experiment and independent evidence

1. Provision a fresh owned System through existing HTTP lifecycle and verify the
   exact Resource, TLS endpoint/domain UUID, provider and image identity.
2. Start the fixed provider-side loopback listener through pinned operator SSH.
   The helper binds an ephemeral port, reports it and waits with a hard lifetime
   bound. It accepts exactly one peer; no NBD handshake is sent. stdin EOF or
   release closes the accepted peer and listener. No persistent service/file.
3. Spawn one client process that opens production Resource-specific TLS and
   submits blockdev-add with generated safe node name and listener endpoint.
   Require remote accepted acknowledgement and confirm the client is still alive.
4. Start the production fresh quiescence probe in a separately supervised process.
   Record entry at its actual fresh-connection/QMP boundary using a transparent
   observer around the external call, preserving production responses. Require
   that entry event, then verify no successful absence acknowledgement while the
   accepted NBD connection remains held.
5. Terminate and join only the exact spawned mutation client. Keep the accepted
   provider peer held; require the fresh probe still cannot acknowledge absence.
   Client termination alone must not be treated as provider quiescence.
6. Release the NBD peer, then require the fresh probe to finish within its bound
   with QuiescenceEvidence matching provider, actual Resource, domain, QOM ID,
   absent result and fresh-qmp-connection ordering. Observe the generated block
   node is absent after failed NBD initialization. No successful node is leaked.
7. Close helper stdin, wait for helper termination, assert listener no longer
   accepts, and join both local processes. Existing System cleanup proves absent
   domain/overlay, released Allocation and restored capacity. Preserve source
   image, historical proofs, keys, TLS and unrelated domains.

Use process supervision so a blocked native libvirt call cannot leave a Python
thread hanging indefinitely. Bounds: listener total lifetime 60 seconds; each
ready/accepted acknowledgement at most10 seconds; monitor start at most10;
held acceptance window at least0.5 seconds both before and after client death;
release-to-probe completion at most15; process termination/join at most5 each.
All are monotonic per-experiment bounds. Timeout fails, triggers owned cleanup
and retains diagnostics; a retry is a new measured attempt, never proof of the
failed attempt. Cleanup must release the provider barrier before killing the
probe or tearing down its domain, avoiding a libvirt job deadlock.

## Contract and evidence

Preserve all24 original failure-resource cells: x86 six orchestration cells
(three failures × two providers) become #3117, x86 six resource cells become
#3118, POWER12 become #2818. Their IDs/scenario IDs/assertions and unbound status
remain unchanged. The new native remote-quiescence cell has assertions
accepted-mutation, client-terminated, fresh-monitor-ordering and cleanup;
inputs image_sha256; deployed roles server/worker/reconciler because normal
System provisioning and release are exercised. Bind the existing node for x86;
leave its POWER counterpart pending/unbound, never satisfied by x86 evidence.

Use existing CellRun/EvidenceWriter/run_cell identity and result accounting.
Record exact candidate, matrix, three-role revisions, observed provider/guest
architecture, KVM, base-image digest, Resource/domain binding digest, ordered
milestones and cleanup. No hostnames, IPs, URIs, usernames, TLS references,
private paths or secrets in published artifacts. Raw diagnostics remain private.
The added cell does not claim the remapped interruption/resource-limit scenarios.
Required preflight absence yields blocked evidence; no unconditional skip remains
once the remote lane is configured. Existing format and qualifier stay unchanged.

## Failure model

Actors and deployments:
- Trusted operator on exclusive client/provider x86 task fixtures, real TLS
  libvirt, actual QEMU/KVM, real backing services and candidate-matched roles.
- Provider/QEMU and network failures during a deliberately blocked operation.

Invariants and assets:
- A successful absence result follows actual provider operation completion.
- Resource/endpoint identity is exact; no command reaches a foreign Resource.
- Only owned client processes, listener, System and overlay are cleaned up.

Accepted failure classes:
- Infrastructure or product failures produce failed/blocked evidence and prevent
  completion; they are not accepted rejections or proof from a subsequent retry.
- The bounded held windows demonstrate this controlled ordering experiment,
  not exhaustive concurrency correctness or durable finalization.

Covered elsewhere:
- Worker/retry/backing-service faults #3117; resource limits #3118; POWER #2818.
- Durable finalization #2681 and excluded installed-authority work remain separate.

## Threat model

Existing TLS secret resolution and redaction remain production-owned. Test-only
SSH carries fixed trusted Python, not caller-supplied commands; validate generated
names/port and exact Resource mapping before invoking QMP. Listener binds only
provider loopback and has a fixed lifetime; EOF releases it. No permission
widening, permanent ports or arbitrary remote filesystem operations. The trusted
operator may mutate only its new domain; unrelated host administrators and a
compromised privileged host are outside this test evidence model. It is not
remote attestation. Sanitize evidence at construction rather than redacting an
unbounded dump later.

## Validation

Focused tests execute real orchestration with substituted external process/SSH/
libvirt boundaries. Controlled early successful probe, absent acceptance event,
still-running mutation client, wrong Resource/UUID, malformed listener messages,
post-release timeout and cleanup failure must each fail. Assert barrier release
precedes domain teardown even when body assertions fail. Contract tests verify
exact old IDs survive with only approved owner changes, new binding resolves to
a collected node, and POWER remains unbound. No wording-only tests.

Run focused tests, lint, whole-tree type, dependency guard, docs and commit hooks;
then independently reviewed exact candidate on separately assigned provider and
client, with native actual ordering and terminal cleanup recorded. Mandatory
managed pre-push owns the full ordinary gate; final/security review and green
remote CI precede authored handoff. Never merge from this worker.

## Verified native binding prerequisites

The production remote opener returns a native `libvirt.virDomain`, which has no
`qemuMonitorCommand` method. The quiescence implementation must call
`libvirt_qemu.qemuMonitorCommand` on that handle, following the existing local
monitor seam. The default production assembly uses this native function; tests
must not invent a domain method that the installed binding lacks.

Native libvirt rejects an application-supplied top-level QMP ID and supplies its
own correlated ID. Both replies require a nonempty native string ID. Only the
`object-del` reply may accept the observed `GenericError` whose description
exactly names the requested object as absent, without a simultaneous `return`.
Other errors, transport exceptions, malformed responses, missing IDs and retained
QOM objects fail. The fresh connection, Resource identity, ordered detach/query
and complete member validation remain required.

These corrections are prerequisites of this proof. Related native-call defects
in remote traffic capture/reaping and debug transport reset remain with #3116
and #3113 respectively; this change does not replace those planes.
