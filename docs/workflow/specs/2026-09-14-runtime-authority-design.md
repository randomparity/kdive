# Provider-owned authority routing (#2473)

Status: Operator-approved and independently reviewed design (2026-09-14).
Scope: issue #2473, WORK:SCOPE token `q2473-7e93b2c1`.
Lane: full-spec; fixed design denominator: 1000 changed lines (L).

## Problem

`ProviderRuntime.authority` already carries a narrow resource-bound sender, but
`jobs/external_boot_authority_client.py` independently resolves local and remote
configuration. `jobs/authority_sender.py` constructs concrete transports and decodes
remote request models. Remote-module services mix database evidence with libvirt
storage and appliance operations. Extending only the client factory leaves that
second ownership problem intact.

## Scope and ownership

The approved charter requires runtime routing (C1), neutral shared orchestration
(C2), local/remote composition (C3), and integration proof of valid and refused
bindings (C4). This design changes their internal ownership while preserving
ADRs 0063, 0606, and 0612's existing runtime and security contracts.

| Responsibility | Intended owner | Migration |
| --- | --- | --- |
| Resource-selected authority identity and sender | Existing runtime authority capability | Local and remote composition provide the capability; worker factories consume it |
| Destination, TLS reference configuration, concrete transport | Provider composition/adapters | Remove local/remote configuration branches from job clients and sender construction |
| Closed envelope encoding and active credential borrowing | Worker sender | Preserve the worker assembly accessor and borrow only during encoding |
| Remote wire requests and response codecs | Remote provider adapter | Shared callers pass neutral requests and receive validated neutral results |
| Module attempt receipts, evidence commits, System verification and cancellation ownership | Live authority-preparation services | Keep repository calls and transaction ordering; consume provider ports |
| Unused service module runtime and phase facade | Removed | No production callers exist; preserve live provider-host configuration and recovery proof |
| Volumes, appliance requests, attachment inspection and libvirt marker operations | Existing remote provider implementation | Keep the live authority-host operations behind its adapter |

The affected caller closure includes job assembly, external-boot runner and lifecycle,
System-authority sender callers, provider composition, module preparation/operation/phase
services, and their direct provider-host and test consumers. Remove obsolete internal
imports and entry points during migration rather than adding compatibility aliases.

Exclusions approved by the operator on 2026-09-14: new provider families or
transport/address-family support; changes to MCP/wire formats, persisted schemas,
authorization policy, credential lifetime, or lifecycle/recovery semantics; unrelated
provider/service restructuring. Project maintainers own each through separate work.

## Selected design

Extend the existing runtime authority surface into a resource-bound capability with
an authority instance, its typed sender, and optional module-operation support.
Retain the current runtime's absence default. The resolver continues to bind the
runtime to the allocated Resource; local composition constructs its fixed local route.
Remote rebinding closes over that Resource's immutable configuration. Sender-free server
composition exposes fixed instance and reservation geometry through the same capability;
worker composition additionally supplies the sender. The existing
`services/external_boot/routing.py` helpers consume that neutral metadata, retaining
their current absence/error semantics. Their direct callers include worker install,
runner preparation, admission, and MCP Run steps; no worker credential is needed for
server-side identity or reservation checks.

The operation-bound client validates provider kind, Resource identity where required,
authority instance, System, activation, Run, plan, and purpose using the existing checks.
It obtains the sender from the capability without reading provider configuration.
Missing or mismatched routes fail before authority allocation or provider mutation.
The fixed local recovery-orphan route remains composition-owned because its request
can outlive the Resource binding; it does not acquire caller-selected routing.

Provider adapters construct remote request models and decode responses. Neutral
module inputs and results expose the identity, receipt, phase, counts and evidence
the service actually checks. Canonical provider records remain validated provider
payloads; adapters preserve their exact bytes and hashes. Shared code does not parse
libvirt volume geometry or import provider-private models merely to forward them.

Remove the unused `RemoteModuleOperationRuntime`, `remote_module_phases` facade, and
their exclusive `ModuleOperationRuntime` protocol. Source-wide symbol/import inspection
found no production caller: the live runner uses `remote_module_authority_preparation`.
Retain the provider operation module's configuration dataclasses used by the live
authority host. Preserve meaningful old recovery assertions against that live path;
do not add replacement adapters for test-only orchestration. The live services retain
repository gates and receipt discharge; the existing provider host retains physical
operations and returns neutral validated observations through its adapter.
The preparation executor is consumed through its generic completion interface; its
concrete construction and shutdown stay with the provider operation's owner.

Request encoding continues to borrow the current worker credential at each call.
Runtime construction and Resource rebinding resolve no TLS material, open no transport,
and make no provider call. The existing per-call transport lifecycle stays unchanged.
The shared client preserves its invocation deadline; service completion loops retain
their existing retry and cancellation behavior after ambiguous dispatch.

## Alternatives

- Extend the existing runtime capability: selected; one binding owns routing and the
  service/provider split retains the architecture's database ownership.
- Add a parallel route registry: rejected as unnecessary duplicate routing policy.
- Move whole remote-module services into the provider: rejected because it also moves
  database transactions and durable evidence ownership, exceeding the needed correction.

## Success

- External-boot and System-authority worker factories use the resolved runtime route.
- Affected shared job/service modules have no direct imports of remote-libvirt config,
  private authority models, or libvirt appliance/storage implementations.
- Local and remote composition supply the appropriate capability; unsupported providers
  retain absence and fail closed when authority is required.
- Existing wire bytes, identity hashes, deadlines, credential borrowing, and durable
  module sequencing remain unchanged in the affected operation paths.

## Failure model

1. Actors and deployments:
   - Fixed workers and provider-host authority in local and remote-libvirt deployments.
   - Authenticated tenants triggering external boot; operators configuring Resources.
   - CI with disposable database and simulated transport/provider boundaries.
2. Invariants and assets at stake:
   - Resource-bound destinations and per-call worker credentials.
   - Exact authority marker identity and canonical durable evidence.
   - System-lock retention through completion and evidence-before-deletion ordering.
3. Accepted failure classes:
   - Optional authority configuration absent: existing fail-closed authority behavior;
     unrelated lifecycle operations remain usable as required by ADR-0612.
   - Unavailable host/TLS material: existing bounded operational error behavior.
4. Covered elsewhere:
   - Listener authentication and journal fencing: existing authority service guards/tests.
   - Persisted schema transitions: existing repository and migration tests, unchanged here.

## Threat model

- Boundary inventory: existing worker-to-authority frame, configuration-to-bound-runtime,
  authority-response-to-service-evidence, and service-to-provider-operation boundaries.
  No new network endpoint or widened operation authorization is introduced.
- Actor model: tenants can influence admitted operations and payloads, not Resource
  destinations or TLS references. Operator configuration and process assembly are trusted.
- Controls: retain marker checks before use, fixed Resource selection, existing closed
  request encoding, response validation and size bounds, per-call credentials/TLS,
  fenced evidence writes, and bounded/redacted error categories at these boundaries.
- Out of scope: new transports, trust policy, and provider families are excluded by the
  approved charter; listener and repository authorization remain under existing guards.

## Validation

- Routing: composition-to-client tests for local and two distinct remote Resources;
  absent capability, missing resource and mismatched kind/instance/resource must fail
  before a transport, credential borrow, or provider mutation.
- Credential lifecycle: repeated operations borrow the current credential and create
  call-local transports; composing/rebinding and unrelated operations borrow nothing.
- Structural boundary: AST import checks over the affected shared modules fail on direct
  provider-private imports; real assembly tests prevent satisfying this by hiding a branch.
- Module behavior: preparation, terminal evidence, restore/reap, missing scratch fallback,
  cancellation and ambiguous-response tests continue to exercise service-owned DB gates
  through the provider port. Before removing tests exclusive to the unused facade, map
  their intended contract to the live authority-host tests and retain missing assertions.
- Wire compatibility: use existing canonical request/result fixtures to assert unchanged
  serialization and identity hashes through adapters.
- Run focused tests, `just lint`, and whole-tree `just type` while building; `just ci`
  before push. Add no dependency or host prerequisite for this refactor.
- Inspect the live-testing runbook and configured carrier prerequisites before live proof;
  installed authority/server/worker revisions must match the tested commit. Report native
  x86_64 and ppc64le coverage separately, including unavailable arms without claiming proof.

## Delivery and rollback

One PR contains the coupled caller migration. The implementation plan is transient and
is reviewed alongside this specification before the scope audit. No schema migration or
deploy-order change is intended; reverting the code
restores the previous internal ownership while preserving existing records.
