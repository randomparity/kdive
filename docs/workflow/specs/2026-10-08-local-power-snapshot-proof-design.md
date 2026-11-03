# Local power and snapshot HTTP evidence

## Problem and authority

Issue #2815 and epic #2803 require observable guest effects through the public
HTTP surface, not successful envelopes. Current main has direct-provider
snapshot tests but no bound power/snapshot tool cells. The approved campaign
scope is five local x86 tools: control.power and systems.snapshot, restore,
list_snapshots and delete_snapshot. Under operator decision R2 the generated
subset is 100 cells: 20 functional and 80 rejection. None is bound today.

The frozen charter is issue comment 6147838355 (re-freeze of 6054889838 after
R2), token q2815-228c46f9, campaign
73d4edacff-8cf13f64-8877-4610-beaf-496193501043. Human approval on 2026-10-07
was “Approve the scopes and exclusions”. Local traffic/SysRq/crash-watch remain
#3115, remote proof #3116 and native POWER #2818. Preserve their obligations.
No public API, snapshot semantics, provider assembly or permission policy changes
are proposed. ADR-0378 governs snapshots; ADR-0722 governs HTTP evidence and is
amended here under R2.

## Capability rule (operator decision R2, 2026-10-13)

The parked design found sixteen generated snapshot capability-rejection cells
(four tools x two configurations x two exposures) that no local run can pass.
Local composition always advertises supports_snapshots=True and installs the
port (local_libvirt/composition.py), and the handler refuses only when that
boolean is false (mcp/tools/lifecycle/systems/snapshot.py). The contract adds a
capability boundary to every capability-gated tool whatever the verdict
(contract.py, _tool_cells), so a supported capability demanded an impossible
rejection. Remote composition also supports every gated capability, so the same
defect produces 564 unreachable capability cells across #2813, #2814 and #2815.

Operator decision R2 resolves it: the cell kind for a capability-gated tool
follows the provider's support verdict. This PR lands the rule for later PRs:

- Contract: _tool_cells adds the capability boundary only when the variant's
  final verdict is unsupported: the ProviderSupport verdict from
  _capability_variants after _tool_cells applies the ArchTraits fadump
  override. A supported variant keeps its functional cell and gets no
  capability cell. An unsupported variant keeps its unsupported cell (the
  reviewed rule that it cannot be functional) and its capability-rejection cell,
  which proves the refusal. Cell ids and the other boundaries are unchanged.
  Today the only unsupported verdict is fadump on x86_64 (four kept cells, #2814).
- Harness: tool_cells.Boundary gains capability. A capability cell proves its
  boundary with a real-issuer token holding sufficient grants, and requires the
  ADR-0209 refusal: a configuration_error envelope whose data.reason is
  capability_unsupported. configuration_error alone is shared with schema and
  name failures, so it cannot pass. Protected state and cleanup are unchanged.
- ADR-0722: a dated amendment records the rule, the harness evidence and the
  consequence that a provider gaining or losing a capability changes the cell
  set (the contract digest moves, so a stale qualification fails).

Rejected: keeping capability cells for supported verdicts and recording them
unsupported (the qualifier requires rejection and the cell would claim an
untested boundary); proving them through the fault-inject provider (another
provider's denial misstates evidence identity); converting the unsupported
functional cell into the capability cell (changes ids #2814 owns for no
evidence gain).

With R2 the approved local x86 subset is 100 cells: 20 functional and 80
rejection (authentication, authorization, project isolation, validation).

## Approach and alternatives

Extend existing tool-cell and catalog-System frames with one support owner for
power/snapshot scenarios and a thin collected carrier. Keep production tools and
evidence schemas unchanged. Existing native libvirt snapshot observations supply
independent evidence; HTTP supplies the operation and job lifecycle.

Rejected alternatives: importing a collected test module couples collection and
support and violates the dependency guard; duplicating the full stack/client or
provisioning framework creates another owner; directly setting a database System
READY or mutating provider capability fabricates the state under test. A captured
HTTP response alone does not prove power or restoration.

## Ownership and surface

- scripts/coverage_campaign/contract.py: the R2 capability rule above, then a
  remap of the remaining 640 #2815 cells, preserving cell/scenario identifiers.
  POWER first goes to #2818 (320); remaining remote to #3116 (160); remaining
  local traffic/SysRq/watch to #3115 (60); the five local x86 tools stay #2815
  (100). Owning tests assert the exact partition, the removed capability cells
  and that every other cell identity is unchanged.
- tests/integration/live_stack/tool_cells.py: the capability Boundary and its
  refusal-envelope rule, with owning tests in test_tool_cells.py.
- docs/adr/0722-...: the dated R2 amendment.
- scripts/coverage_campaign/obligations.toml: bind only the five tools' local
  scenarios, leaving remote and local traffic/SysRq/watch unbound. A local
  scenario id has no architecture, so its ppc64le cells share the node, as every
  local carrier's do; they stay owner #2818 and are never collected on x86_64.
- tests/integration/live_stack/power_snapshots.py: scenario orchestration and
  independently testable guest/libvirt observations, using existing HttpCaller,
  run_tool_cell, on_lane_system, deep_lifecycle and cleanup owners.
- tests/integration/test_power_snapshot_tool_cells_live.py: thin parametrization
  over the approved local cells; no support logic imported from collected tests.
- tests/integration/live_stack/test_power_snapshots.py and owning coverage-contract
  tests: boundary, lifecycle, restoration-oracle and ownership regressions.
- docs/operating/runbooks/live-testing.md: actual fixture/run/bind/qualification
  instructions and limits. This durable specification; transient plan stays private.

Existing tool_cells/scenario/deep_lifecycle helpers may receive only the minimal
criterion-linked extension needed to reuse their existing lifecycle contracts.
No support/catalog ownership changes from the concurrent #3106 branch.

## Real-System preparation and identity

Use a fresh catalog Fedora 44 System through on_lane_system. Read actual
supports_snapshots advertisement, domain accelerator and guest OS/architecture;
assert x86_64/KVM and a plain local System without authority/external-boot fence.
Missing prerequisites block; no authority installation or database repair.
Reuse the verified immutable longterm kernel fixture and existing build-upload
helper, installing/booting through HTTP. Observe domain kernel digest, guest
release and ELF build ID independently; populate the existing kernel context
fields from verified fixture/upload data. Keep runtime candidate and fixture
builder identities separate. Reconnect using the existing bounded SSH helper.

Build completion makes the Run terminal before restore; independently read its
state and ensure no live Run or debug session remains. Do not force state. If
any operation is still pending, drain its real job with existing deadlines.
Use the existing frame's try/finally cleanup, track the installed boot file and
owned overlay, and prove allocation release, absent domain/disks and restored
capacity. Source image and immutable fixture are never cleanup targets.

## Functional effects

Each cell invokes its named operation in its own direct/gateway exposure and
uses the issuer/configuration proof. Supporting setup calls may use the operator
client, but no supporting success substitutes for the operation under test.

Snapshot state uses unique synthetic values in a task-owned persistent guest
file and a tmpfs file under /dev/shm. Write and sync the persistent value; read
both back before capture. Every persistent read (baseline, after mutation, after
restore) first runs sync and drops the guest page cache (vm.drop_caches=3), so
the value comes from the disk and not from page cache restored with RAM.
Record boot ID and a long-lived process identity.
Create an include_memory checkpoint, drain the job and verify both the durable
available row and native libvirt checkpoint. Change persistent and tmpfs values,
then read them back to prove the mutation occurred. Restore and verify both
original values, the original boot ID and the resumed process identity. A
no-op restore leaves changed values and fails; a reboot-only restore loses
tmpfs/process state and fails; a RAM-only revert leaves the mutated disk value
and fails the cache-dropped persistent read. This observes actual RAM contents without a new
guest service or dependency. No arbitrary guest command supplied by a caller.

- systems.snapshot: perform the restoration sequence to establish that the
  checkpoint retained disk and RAM, rather than asserting only checkpoint existence.
- systems.restore: exercise running and paused restoration; independently observe
  PAUSED and persisted paused state, then resume through control.power and check
  restored values. Also exercise disk-only restore last, on the same System after
  every memory checkpoint is deleted, so no internal checkpoint coexists with
  it: persistent marker rollback and a new boot ID, no memory claim.
  Local disk-only snapshots use VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY without an
  internal disk element, which libvirt may realize as an external overlay; no
  live proof reverts one today. A revert or overlay-cleanup failure is recorded
  as failed evidence and reported as a product finding against ADR-0378, never
  fixed or waived here. Cleanup counts any libvirt-created overlay as owned.
- systems.list_snapshots: create two known checkpoints and compare ordered names,
  available states, include_memory values and native checkpoint inventory. Empty
  output cannot pass the positive control.
- systems.delete_snapshot: create an owned checkpoint, delete through the cell
  exposure and drain its job. Check absence from native inventory and persisted
  listing; reuse the exact name successfully to prove logical reclamation, then
  remove it. Do not infer physical qcow2 shrinking from a deleted checkpoint.
- control.power: exercise on/off/cycle/reset from allowed READY state and resume
  only from a real paused restore. Observe domain inactive/running/paused states,
  persisted state and guest boot ID transitions. Off must finish with inactive
  domain; on/cycle/reset must reach a newly observed boot where applicable;
  resume preserves the captured boot ID and RAM state. No fixed sleep as oracle.

## Rejection effects

Reuse prove_rejection for signature control, role boundary, project isolation
with absent-owner twin and schema-invalid arguments. Rejection cells aim at the
stack's existing lane_target: a torn-down System and released Allocation that no
background work changes, the target the System and run carriers already use.
Each tool resolves the System and checks project membership and role before its
state check, so the torn-down target is valid; a tool that checked state first
would fail its cell rather than pass it. Arguments use the target's genuine id
and valid snapshot names. Authentication cells follow the run carrier's rule: the token holds
viewer on the real target, one rank below the contributor gate, so the
issued-token control passes authentication and is refused by authorization
before enqueue; it can power, restore or delete nothing. list_snapshots is a
read either way. Viewer is sufficient only for list_snapshots; its lower
role control has no project role. Mutating operations require contributor.
Project isolation uses the actual wrapper's failure category, independently
confirmed from current routing. project_state already covers snapshots (it has a
project column). jobs has none, and snapshot, restore, delete and power jobs link
to the System only through payload system_id, so the protected state adds the
target's jobs rows by payload system_id, for every tool including control.power,
and the target's absent libvirt domain (a leaked power or restore would define
or start it). Reject unchanged-state or cleanup claims
when observation fails. None of the five tools has a local capability cell
under R2; the harness capability rule is proven by focused tests built on the
real capability_unsupported envelope, not by a live cell of this issue.

## Failure model

Actors and deployments:
- Trusted operator on an exclusive disposable x86 KVM host, real issuer/backends
  and candidate-matched server, fixed worker and reconciler.
- Deliberately forged, insufficient-role and foreign-project HTTP callers.

Invariants and assets at stake:
- No false evidence of restoration, rejection, runtime identity or cleanup.
- Only freshly owned System/overlay/checkpoints are mutated or deleted.
- Existing images, historical artifacts and other task allocations survive.

Accepted failure classes:
- A real product or environment failure produces failed/blocked evidence and
  retained diagnostics, not a fabricated pass. It does not complete this issue.
- Disk-only snapshots are crash-consistent per ADR-0378; no application-wide
  transactional snapshot claim is made.

Covered elsewhere:
- Native POWER #2818, local traffic/SysRq/watch #3115, remote #3116.
- External authority and active external-boot recovery remain outside this driver.

## Threat model

Boundaries: HTTP authentication/authorization and gateway schema, operator SSH,
worker libvirt/guest observations, database readback and evidence publication.
No new runtime boundary or permission grant is introduced.

Trust the assigned disposable host/operator and verified source fixture, not
successful tool replies. The existing SSH helper authenticates with a fresh
per-cell key but does not pin the guest's host key on the worker's loopback
forward; the disposable host is trusted for that. Fixed commands contain only
generated validated identifiers. Never serialize
DSNs, tokens, hostnames, addresses, private paths or raw guest output into public
evidence. Native observation opens only the configured selected endpoint and
exact owned domain. Database observation uses bound values and readonly queries.
A compromised privileged host and mutually hostile host administrators are
outside this test evidence model; it does not provide remote attestation.

## Verification and completion

Focused regression tests mock only HTTP/SSH/libvirt/process boundaries, execute
real orchestration/oracle logic and assert cleanup even on failure. Controlled
faults must fail for a no-op restore, reboot substituted for RAM restore, missing
checkpoint, RAM-only revert, changed jobs rows or domain state, still-defined domain/disk and wrong
boot/build identity. Ownership tests compare exact old/new sets and ensure no
excluded cell is bound to this local driver.

Run affected tests, lint, whole-tree type, dependency ownership, docs and hooks.
After approved fixture preparation, run direct/gateway × default/recovery cells
against the exact candidate, generate bindings from the verified inputs, assemble
and qualify the exact approved selection. Report observed counts and every
failed/blocked/missing cell. Closed #2809/#2810 provide reusable code, not proof.
Final review/security and mandatory managed pre-push full gate precede delivery.
The PR closes #2815 only when all 100 approved cells qualify against the exact
candidate; any failed, blocked or missing cell keeps the issue open.
