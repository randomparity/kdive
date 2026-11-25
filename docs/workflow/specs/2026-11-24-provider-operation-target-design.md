# Provider-owned operation targets

Decision: [ADR-0867](../../adr/0867-provider-owned-system-handles.md).

## Problem

Generic callers select a provider runtime but still construct operation targets.
Boot evidence and SSH authorization always construct `kdive-<id>`, other worker
paths prefer the recorded name, and server SSH/debug paths use a bare UUID when
that name is absent. Fault-inject actually provisions `fault-inject-<id>`.
The fault-inject profile correctly disables boot-time gdbstub probing; that helper
witness alone is not a runtime-path proof. A stable `provision_handler` replay on a
torn-down System with no committed name returns normally while its real synthetic
domain remains in the inventory. These are existing paths, not a CLI regression.

## Scope

Add one required pure target resolver to `ProviderRuntime`, using the existing
callback/type ownership in `providers/core/runtime.py`. Its inputs are the System
UUID and recorded `domain_name`; its output is the existing opaque `SystemHandle`.
Do not add a provider registry, new target model, default callback or alias.

Migrate the four current compositions and in-tree constructors/fakes. Local and
remote libvirt share a helper in the existing provider naming module. Fault-inject
uses its existing provisioning namespace. The BYO-host skeleton still refuses;
this does not implement its host operations or advertise another capability.

Migrate the existing generic operation callers in systems, devices, control,
SysRq, traffic, SSH, boot evidence and its boot plumbing, plus server SSH endpoint
and debug transport acquisition. Other queued findings retain their current owners.
No public wire/parameter/envelope, applied SQL, deployment or dependency changes.
Console readers/snapshots, Run artifacts and UUID-addressed lifecycle ports retain
their existing identities; an operation target is not a storage or audit key.

## Design

`SystemHandleResolver = Callable[[UUID, str | None], SystemHandle]` is a type alias
beside the runtime's existing callback aliases. The required `system_handle` field
has no generic fallback. Every current runtime constructor states its policy;
resource rebinding reconstructs that field with the selected provider's ports.

| Runtime | Nonempty recorded name | None or empty name |
| --- | --- | --- |
| local-libvirt | preserve exactly | existing `kdive-<id>` |
| remote-libvirt | preserve exactly | existing `kdive-<id>` |
| fault-inject | preserve exactly | actual `fault-inject-<id>` |
| BYO-host skeleton | refuse before I/O | refuse before I/O |

The skeleton uses its existing `NOT_IMPLEMENTED` refuser. Unknown kinds still fail
at existing resolver selection. The callback does no I/O, configuration lookup,
secret resolution, state mutation or caching; it does not choose a resource host.

Keep state/role/authority checks and their order. Helpers that currently discard
the loaded System after deriving its name instead carry the necessary identity
facts until the already-selected runtime can resolve a handle. Bind the target
policy and operation port together without another database resolution.

For force-crash, resolve the handle after the READY check and before committing
CRASHING, within the existing transaction; a refusing resolver cannot leave a new
CRASHING marker. Preserve controller selection before that transaction and the
last-write-before-NMI rule. Keep physical I/O under the existing System fences.

SSH authorization reads the current recorded name, retaining READY enforcement
only for a carried authority port. Missing/non-READY carried-port behavior remains
`system_not_ready`; a missing ordinary System retains resolver-style NOT_FOUND.
No new READY requirement applies to the ordinary path.

Boot probes receive a resolved handle, not a UUID to rename. Thread the resolver
as an explicit required collaborator through the existing evidence functions.
Resolve only inside existing probe gates: no probe after a ready boot, without
console panic, or when the profile lacks a gdbstub. Preserve strict crash-halted
failure propagation and expected-crash best effort, including callback failures.
In server debug acquisition, resolve inside its existing categorized-error mapping.

## Failure model

- Actors/deployments: trusted in-tree runtime assembly and current worker/MCP
  callers on supported Linux x86_64 and ppc64le. Local proofs run on x86_64.
- Assets/invariants: correct provider target, project/resource identity, authority
  and worker fences, guarded lifecycle state, caller attribution, secret-safe
  errors, console/storage ownership and capability truthfulness.
- Accepted classes: metadata may change after an existing snapshot under the
  current locking rules; this adds no cross-operation cache or new atomicity
  promise. Existing transport/provider failures retain their handling.
- Covered elsewhere: provisioning identity creation, provider target validation,
  live VM/hardware qualification and authority-host isolation keep their existing
  proofs. Scoped fake-boundary/disposable-DB tests are not hardware qualification.

## Success

1. The four current assemblies supply the required policy, including after
   resource rebinding. Constructor omissions fail; no generic fallback survives.
2. Boot and SSH paths honor the recorded name and applicable legacy fallback.
   Superseded provision/teardown uses fault-inject's actual namespace when no name
   was committed. Its existing disabled boot-probe gate stays disabled.
3. Other named worker operations and the two server paths use the same policy,
   retaining existing authorization/state/capability errors and port forwarding.
4. Callback refusal cannot trigger protected I/O or a new CRASHING marker;
   expected-crash probe gates and failure policy remain intact.
5. Console UUIDs, artifact keys and existing lifecycle UUID parameters are unchanged.
6. Generic callers no longer construct SystemHandles or import naming fallbacks.

## Validation

Each Success item has focused executable/structural proof in the transient plan:
provider composition/rebinding matrices; stable worker-handler RED/GREEN paths;
control/device/SSH/SysRq/traffic/snapshot/teardown and server transport tests;
callback-refusal/probe-gate/state tests; existing console/artifact/lifecycle proofs;
and an import/construction boundary guard with restored controlled faults.

Run the scoped provider/worker/MCP suites with actual disposable Postgres where
selected, plus lint, whole-tree types and hooks. Keep public wrapper schemas,
protected unrelated functions and all 198 applied SQL unchanged. Require fresh
independent design, code and security review. Commit and attribute the actual
source fix; record a later legitimate changed-source assessment without rerolling
unchanged source. Final campaign whole CI and whole-branch reviews remain required.
