# KDIVE Production Architecture Design

## Purpose

KDIVE is a production, multi-user service that gives agentic coding environments
(Claude Code, Codex) a complete Linux kernel development and debug lifecycle.
Local VMs are the default provider. Remote libvirt is an operator-configured
provider; remote bare metal, PowerVM, and cloud providers remain future work.

KDIVE was implemented as a greenfield Python rewrite of a single-user, local,
stdio proof of concept. Python provides native access to the kernel-tooling
ecosystem (drgn, libvirt bindings, crash, and the MCP SDK).

## What changes from the PoC

| Concern | PoC | Production |
|---|---|---|
| Tenancy | single-user, local | multi-user hosted service |
| Transport | stdio | MCP over streamable HTTP |
| Central abstraction | run-centric (a run bundles build+boot+debug) | six durable objects with independent lifecycles |
| State | per-run JSON + flock | Postgres (system-of-record) + S3-compatible object store |
| Identity | implicit local user | OIDC/SSO + RBAC, with on-behalf-of agent attribution |
| Accounting | none | metering ledger + enforced budgets/quotas (admission control) |
| Long-running ops | inline | durable job queue + worker tier |
| Resource scope | local x86_64 libvirt only | typed multi-provider runtime selected by resource kind |

## Core decisions

These decisions define the implemented architecture. Accepted decisions and
their later amendments are recorded in the [ADR collection](../adr/).

1. **Greenfield rewrite**, Python.
2. **Multi-user service**; MCP over streamable HTTP.
3. **Six durable objects** (Resource / Allocation / System / Investigation / Run /
   DebugSession), replacing the run-centric model.
4. **First slice targets local libvirt/QEMU** — proven infra, on the new
   architecture, before remote/cloud/bare-metal.
5. **Postgres + object store** for state; Postgres advisory locks replace flock.
6. **OIDC/SSO + RBAC** with `(principal, agent_session)` attribution.
7. **Metering + budgets/quotas** with an admission-control gate on allocation.
8. **Async worker tier + durable job queue**; hard per-tenant sandboxing
   designed-for but deferred.
9. **Typed provider runtime ports** across narrow per-plane interfaces, with active
   `ResourceKind`-based dispatch for local-libvirt, fault-inject, and configured remote-libvirt
   providers; capability-registry dispatch and additional provider families remain future options
   (ADR-0063).

## System topology

```
                  agent (Claude Code / Codex)                human (CLI)
                            │ MCP (streamable HTTP)              │ MCP (streamable HTTP)
                            ▼                                    ▼
        ┌───────────────────────────────────────────────────────────────┐
        │                    API / Orchestration Core                    │
        │  • MCP tool surface  • authz (OIDC/RBAC, on-behalf-of)         │
        │  • lifecycle state machines  • admission control (quota/budget)│
        │  • job dispatch  • response shaping (snippets+refs, not dumps) │
        └───────────────┬───────────────────────────┬───────────────────┘
                        │ enqueue jobs              │ read/write state
                        ▼                           ▼
        ┌──────────────────────────┐    ┌──────────────────────────────┐
        │   Durable job queue       │    │  Postgres (system-of-record) │
        │  (provision/build/install │    │  resources, allocations,     │
        │   /debug-op/control jobs) │    │  systems, investigations,    │
        └───────────┬──────────────┘    │  runs, reservations,         │
                    ▼                    │  accounting ledger, audit    │
        ┌──────────────────────────┐    └──────────────────────────────┘
        │   Generic worker fleet    │    ┌──────────────────────────────┐
        │  run provider operations  │───▶│  Object store (S3-compatible)│
        │  operation dispatch lanes │    │  vmcores, build outputs,     │
        └───────────┬──────────────┘    │  console/gdb transcripts     │
                    ▼                    └──────────────────────────────┘
   providers: local-libvirt │ fault-inject │ remote-libvirt │ cloud │ baremetal-bmc │ powervm …

     Kubernetes API ── bounded Pod authority ──▶ lifecycle-witness ──▶ Postgres
       (Kubernetes-only)                                worker-incarnation state
     worker init ────── mTLS credential broker ────────────▲
       projected Pod token; one credential delivered to tmpfs
```

This is the live topology. A future UI may introduce another protocol only after its public
surface is designed and implemented; it is not part of the current ingress contract.

- **MCP over streamable HTTP** — the service is remote and multi-user; agents
  authenticate with scoped, on-behalf-of tokens.
- **Thin, fast core** — owns state machines, authz, admission control; dispatches
  work and never blocks on a long provision.
- **Worker tier** — one generic fleet pulls jobs from a durable queue; long-running ops are jobs
  with pollable status. Dispatch lanes separate default work from state-fenced lifecycle work.
  Resource-class pools remain a future isolation option; production does not route or deploy
  workers by resource class. Hard per-tenant sandboxing is deferred.
- **Lifecycle witness** — a platform-optional, Kubernetes-specific authority process, separate
  from the server, worker, and reconciler. The shipped Helm chart always deploys it as a fourth
  singleton control-plane workload; the reference Compose deployment instead uses an operator-run
  lifecycle gate. It binds worker incarnations to exact Pod UIDs, delivers init-only credentials,
  records terminal evidence in Postgres, and only then removes the Pod finalizer. It accepts only
  `Succeeded` or `Failed` Pods at configured StatefulSet ordinals, persists the exact `(namespace,
  name, UID)` active-to-terminated transition, then removes the finalizer with a UID-,
  resourceVersion-, and finalizer-value-fenced JSON Patch. A missing registration, API or database
  failure, or patch conflict retains the finalizer for retry. It is not a general job worker or
  drift reconciler.
- **Postgres = system-of-record** for structured state and accounting/audit
  ledgers; **object store** for bulk artifacts, referenced by row.

The lifecycle witness owns deliberately narrow state and trust boundaries. Its dedicated Postgres
role may register and terminate Kubernetes worker incarnations and read or acknowledge their
encrypted credential envelopes. Its dedicated service account may read and patch finalizers only
on the configured, bounded worker Pod names and may submit TokenReviews. Only this process receives
the witness database credential, credential-broker TLS private key, envelope key, and the
service-account authority to read or patch those Pods and submit TokenReviews. The worker init
container receives only a separate, short-lived broker-audience token and the broker CA; the
long-running worker receives neither that token nor the private key or envelope key. The server and
reconciler receive none of the witness authority. A witness failure therefore retains finalizers
and blocks new worker credential delivery rather than accepting unaudited cleanup evidence.

## Domain model

Six durable objects. Within the Resource → Allocation → System → Run chain, lower
layers outlive higher ones; **Investigation is a cross-cutting grouping** whose
lifetime is independent of any single Allocation (see below). Each is a Postgres
row with an explicit state machine.

```
(principal / project) ──< Investigation ──┐
                                          ├──< Run ──< DebugSession
   Resource ──< Allocation ──< System ────┘
```

A Run is the join point: it belongs to exactly one System (which fixes its
Allocation) and exactly one Investigation (which may group Runs across many
Allocations).

### Resource

A bookable thing, registered by a provider; long-lived, possibly shared.

- Fields: `id`, `provider`, `kind` (local-libvirt / remote-libvirt / cloud /
  baremetal-bmc / powervm), `capabilities` (arch, CPU model+count, memory, disk,
  PCIe devices, console/control transports: SoL/IPMI/Redfish/HMC/gdbstub),
  `pool`, `cost_class`, `status` (available / degraded / offline / draining).
- Resources are discovered or registered, not created by a run. State is mostly
  health/availability.

### Allocation

A user's claim on a Resource for a window. Authz, admission control, and
accounting live here.

- States: `requested → granted → active → releasing → released`, plus `denied`,
  `expired`, `failed`.
- `requested → granted` passes through **admission control**: selector/resource fit,
  RBAC, quota/budget check, **and a capacity check against host headroom**.
  Local-libvirt is "always-yes" only for *chargeback/reservation* — it is still
  capacity-admitted (a concurrent-System cap or resource accounting) so M0/M1 fail
  closed instead of thrashing the single host. Cloud/lab adds a real
  reservation/lease with a chargeback estimate.
- Carries `lease_expiry`, `(principal, agent_session)`; emits accounting events
  on every transition.

### System

A provisioned, bootable instance produced by applying a provisioning profile to
an Allocation.

- States: `defined → provisioning → ready → reprovisioning → failed → torn_down`.
- Identity = (allocation, provisioning profile, resulting OS/target fingerprint).
- One Allocation can host sequential Systems (reprovision in place). A System
  never outlives its Allocation.
- **Installing a new kernel and rebooting does not make a new System** — only an
  OS reprovision does.

### Investigation

A campaign that groups the Runs iterating toward a goal — a bug fix or a feature.

- States: `open → active → closed`, plus `abandoned`. `investigations.open`
  creates it `open`; it becomes `active` when its first Run is created. Closing is
  explicit (the agent resolves the bug or gives up); the reconciler moves an
  Investigation idle past a retention window to `abandoned`. Neither closing nor
  abandoning cascades to its Runs — they stay queryable for narrative and cost
  audit, and any still-in-flight Run keeps running under its own Allocation until
  it reaches a terminal state (the Investigation is a grouping, not a resource
  owner).
- Scoped to a `(principal / project)`, **not** to a single Allocation. Groups the
  sequence of Runs; carries narrative/notes, external references (e.g. Bugzilla/JIRA), and rolled-up cost attribution.
- **May span System reprovisions, Allocations, and resource kinds**: if the chase
  moves from a local VM to bare metal — a new Allocation on a different Resource —
  the Investigation continues. Each Run records which System it used, and cost
  attribution **rolls up across allocations and `cost_class` boundaries** in a
  single normalized unit (reference cost-model units, not raw wall-clock), so a
  local-VM Run and a cloud Run sum meaningfully. The cost-model coefficients and
  how `cost_class` is assigned per Resource are an ADR-0007 concern.

### Run

One kernel-version attempt: build patch vN → install → boot that kernel → debug
it.

- States: `created → running → succeeded / failed / canceled`.
- **Idempotent steps** keyed by `run_id` + step (the one PoC invariant kept).
  One build per Run keeps this clean.
- The agent's real loop is **many Runs against one persistent System**, each Run
  carrying at most one DebugSession (per boot). Allocation and provisioning
  happen once; iteration is cheap.

### DebugSession

A sub-object of a Run, bounded by a single boot of a single kernel.

- States: `attach ↔ live ↔ detached` — within one boot the session may re-attach
  after detaching (and interrupt/continue) any number of times; the cycle ends
  only at reboot.
- **A durable row**, not just worker-side state: persists `(state, transport
  handle, worker heartbeat)` so the reconciler can detect a `live` session whose
  transport has died and move it to `detached` (see Reconciliation & teardown).
- A **reboot ends it**: the transport drops and, for a patched kernel, symbols
  and addresses change. The next attach after a reboot is a new DebugSession
  belonging to the next Run.

### Carried invariants (generalized from the PoC)

1. **Immutable request inputs** per object once created (the profiles that
   defined it).
2. **Idempotent, lock-guarded step execution** — Postgres row / advisory locks
   replace flock; serialization is per-Allocation and per-System.
3. **A Run's Allocation is determined by its System** (`run.system → allocation`).
   The Investigation grouping a Run imposes no allocation constraint — it may
   group Runs across different Allocations and resource kinds.

## Provider model

Providers are the extension seam.

### Current status

In M0/M1 the production seam is
`ProviderRuntime`: startup builds typed ports for each configured provider
(`Provisioner`, `Builder`, `Installer`, `Controller`, `Retriever`, debug and
introspection ports) and passes those ports to MCP tool registrars and worker
handlers. Production defaults to the local-libvirt runtime. Remote-libvirt is an implemented,
operator-configured production provider that drives guests on separate libvirt hosts; fault-inject
is another concrete provider, enabled only by explicit profiles for test and failure-path coverage.
Runtime selection flows through `ProviderResolver`, so tools and handlers resolve the provider
attached to the Allocation or System instead of assuming local-libvirt. Cloud, bare-metal, and
PowerVM providers remain future work on this typed runtime seam. Composition is centralized in
`src/kdive/providers/assembly/composition.py`.

The capability registry from ADR-0009/ADR-0022 is historical design context, not an
in-tree prototype or the live dispatch path. It is not used for job routing,
destructive-op gating, or reconciler behavior in M0/M1. ADR-0063 records this narrowing
and ADR-0066 removed the prototype source so contributors extend the runtime that actually
serves requests.

## Lifecycle planes

| Plane | Responsibility | Implemented providers | Future providers |
|---|---|---|---|
| Discovery | register resources, advertise capabilities, report health | local host enumeration; configured remote-libvirt hosts | cloud regions, lab inventory, HMC frames |
| Allocation | claim/lease/release; feeds admission control + accounting | core capacity-checked allocation for local and remote resources | cloud reserve API, lab reservation, LPAR activate |
| Provisioning | apply a provisioning profile → a ready System | local and remote libvirt domain + rootfs provisioning | ISO+kickstart, image bake, NIM/PXE |
| Build | produce a kernel from source + profile | local build; remote-libvirt in-guest build helper | cloud builders, hosted CI workflows |
| Install | deploy a built kernel onto a System | local direct-kernel install; remote-libvirt guest helper | image bake, netboot |
| Connect | establish a debug/console transport | local and remote libvirt gdbstub, SSH, and console paths | SoL, KGDB-over-serial, BMC console |
| Debug | constrained debug ops over a transport | gdb-MI and drgn for local and remote-libvirt Systems | crash, KDB |
| Control | power/reset/force-crash | local and remote libvirt control paths | IPMI/Redfish power, HMC, NMI |
| Retrieve | pull debug artifacts | local and remote-libvirt vmcore retrieval | BMC SOL capture, cloud-native artifact retrieval |

**Ported from the PoC behind these interfaces:** redaction, path safety,
constrained-debug allowlist, gdb-MI tier, drgn introspect/vmcore, crash
postmortem, run-readiness preflight.

### Artifact and catalog package ownership

These packages are related but not interchangeable:

| Package | Responsibility |
|---|---|
| `kdive.artifacts.storage` | Cross-cutting object-store request and result contracts. |
| `kdive.artifacts.uploads` | Upload declarations, manifests, content addressing, reassembly, encoding, and write leases. |
| `kdive.artifacts.catalog` | Artifact-row persistence, read models, discard, and etag repair. |
| `kdive.artifacts.formats` | Format-specific artifact parsing, currently pcap packet counting. |
| `kdive.store` | Object-store clients and environment-backed store assembly. |
| `kdive.build_artifacts` | Build-output result shapes and build-id validation. |
| `kdive.kernel_config` | Uploaded kernel-config parsing, effective-config fetch, and feature requirement gates. |
| `kdive.components` | Typed component refs and config-requirement validation. |
| `kdive.images` / `kdive.inventory` | Image inventory, catalog reconcile, and TOML shape. |
| `kdive.mcp.tools.catalog.artifacts` | Agent artifact tools and upload/download authz. |

Provider build semantics, provider filenames, S3 upload mechanics, and MCP response shaping stay
outside these data-owner packages unless the package above names that responsibility.

Historical build-config catalog designs live under `docs/archive/design/`. They were superseded
by [ADR-0316](../adr/0316-remove-server-build-lane.md); `kdive.build_configs`, `buildconfig.*`,
and the build-config catalog are not part of the live architecture.

## MCP tool surface

Atomic primitives mapped to planes. Every tool returns structured JSON with the
relevant object id, status, `suggested_next_actions`, and artifact **references** —
never log dumps.

**Long-running operations use an explicit job model.** Provision, build, install,
capture-vmcore can run 30+ minutes. Those tools enqueue a job and return
`{job_id, status: "running"}`; the agent polls `jobs.get` (or `jobs.wait` with a
timeout). Fast ops (set breakpoint, read memory, power state) return directly.

```
Discovery / selection
  resources.list(filter)              → resources + advertised capabilities
  resources.describe(resource_id)     → full capability detail, health, cost_class

Allocation                            (admission control + accounting)
  allocations.request(selector, window, project)  → granted | denied | job
  allocations.list / .get / .release
  accounting.estimate(selector)       → cost estimate before committing
  accounting.usage(project|principal) → ledger rollup, budget remaining

Provisioning
  systems.provision(allocation_id, provisioning_profile)   → job → system_id
  systems.define / .provision_defined
  systems.get / .reprovision / .teardown

Investigation + Run
  investigations.open(project, title)         → investigation_id
  runs.create(investigation_id, system_id, build_profile, …)
  runs.build(run_id)    → job        runs.complete_build(run_id)
  runs.install(run_id)  → job        runs.boot(run_id) → job
  runs.get(run_id)

Connect + Debug
  debug.start_session(run_id, transport)   debug.end_session
  debug.set_breakpoint / .clear / .list
  debug.continue / .interrupt
  debug.read_registers / .read_memory(≤4096)
  introspect.run / .from_vmcore         postmortem.crash / .triage

Control + Retrieve                    (destructive → policy gate)
  control.power(system_id, on|off|cycle|reset)
  control.force_crash(system_id)
  artifacts.list(system_id) / .get(artifact_id) / .search_text(artifact_id, pattern)
  artifacts.create_run_upload / .create_system_upload
  vmcore.list(system_id) / .fetch(system_id) → job

Jobs (long-running spine)
  jobs.get(job_id) / jobs.wait(job_id, timeout) / jobs.cancel(job_id) / jobs.list
```

- Agents drive workflows plane-by-plane, which matches how they iterate on a patch.
- `jobs.*` is the uniform async spine: every long-running tool returns the same
  job-handle shape, so the agent learns one polling pattern.
- `debug.read_memory` keeps the PoC's 4096-byte cap.

## Cross-cutting concerns

Applied across every plane.

- **Secrets by reference** — cloud creds, BMC/IPMI passwords, SSH keys, sudo,
  HMC tokens never appear in requests, state rows, or responses. The service
  resolves references from a pluggable secret backend at the worker boundary;
  only `(present, source-ref)` is persisted. When a worker resolves a reference,
  it **registers the resolved value into the process-owned redaction registry**
  passed through runtime composition (ADR-0327) for the op's lifetime, so any transcript or
  console output capturing the value is masked by **exact-value replacement**, not
  merely by the redactor's secret-name patterns. Output captured before
  registration completes is quarantined (object-store, sensitive) until redacted.
- **Mandatory redaction** — all guest output, gdb/SoL transcripts, and console
  logs pass through the redactor before persistence and before any response
  snippet. Raw artifacts stay in the object store, marked sensitive, fetched only
  by explicit `artifacts.get`.
- **Audit log** — every state transition and every destructive op writes an
  append-only audit row attributing `(principal, agent_session, tool,
  args-digest)`.
- **Accounting ledger** — allocation transitions emit usage events; admission
  control checks budget/quota on `allocations.request` and denies or requires
  approval over budget. The budget/quota **check and the resulting ledger debit
  are atomic** under a per-project lock (see Concurrency) — otherwise two
  concurrent requests can both pass the check and overspend.
- **Service-layer boundary** — `kdive.domain` owns pure domain models, state
  machines, and cost/lease rules. DB-coordinating workflows that compose locks,
  repositories, idempotency rows, audit rows, and ledger writes live in
  `kdive.services` (for example allocation admission, renewal, and accounting
  rollups), so persistence orchestration is not hidden inside domain modules.
- **Destructive-op policy gate** — `force_crash` requires both the allocation
  project's RBAC role and explicit profile opt-in. Power, teardown, and
  reprovision use their own lifecycle and RBAC checks; they do not use the
  force-crash profile gate (ADR-0130).
- **Concurrency** — serialize per-Allocation and per-System via Postgres advisory
  locks; idempotent steps keyed by `run_id` + step. Admission control serializes
  on a **per-project (budget-scope) lock** — an advisory lock or `SELECT … FOR
  UPDATE` on the budget row — so the check-then-debit on `allocations.request`
  cannot race.

### Reconciliation & teardown

State in Postgres can drift from real infrastructure whenever a worker dies, a
lease expires mid-operation, or a `jobs.cancel` lands on a half-applied op. A
periodic **reconciler loop** in the core detects and repairs that drift:

- **Orphaned Systems** — a System whose Allocation is `released` / `expired` /
  `failed` is torn down (a System never outlives its Allocation).
- **Runs on torn-down Systems** — a Run whose System is torn down has its
  in-flight job canceled and the Run marked `failed` (`lease_expired`). The Run
  row is **retained, not deleted**, so the Investigation's cross-allocation
  narrative and cost rollup stay intact even though the Run's Allocation is gone.
- **Abandoned jobs** — each job carries a **worker heartbeat/lease**; when it
  lapses the job is marked abandoned and the op's declared compensation runs.
  (Advisory locks release on connection close and the PoC's `O_CREAT|O_EXCL` lock
  releases on unlink — but neither cleans up *infrastructure*, only the lock.)
- **Dead DebugSessions** — a session row in `live` whose transport is unreachable
  is moved to `detached`.
- **Leaked provider infra** — the reconciler reconciles against typed provider
  inventory/reconcile operations to find, e.g., a libvirt domain with no owning
  System row.
- **Idle Investigations** — an Investigation in `open` / `active` whose last Run
  was created beyond the retention window is moved to `abandoned`. Closure is
  otherwise explicit, and abandoning never cascades to its Runs.

**Lease-expiry policy.** On `lease_expiry`, in-flight jobs are drained within a
grace window, then force-killed; the owning Run transitions to `failed`
(`lease_expired`) — distinct from a `canceled` Run, which records an explicit
`jobs.cancel` or agent abort, so audit and SLO tracking can tell an
infrastructure kill from a deliberate one. The accounting ledger attributes the
partial spend to the Allocation regardless of completion. **Cancel/abandon cleanup** is
part of each typed worker operation's policy: each op declares in code whether cancel yields
clean-rollback, best-effort, or orphan-flagged state — `jobs.cancel` on a half-done
`provision` / `install` is never undefined. ADR-0063 narrows the M0/M1 provider seam to typed
runtime ports; the historical capability-registry design does not drive this behavior.

## Error taxonomy

Keep the PoC's stable, agent-facing `ErrorCategory` taxonomy and extend it for
the new planes: `configuration_error`, `missing_dependency`, `build_failure`,
`boot_timeout`, `readiness_failure`, `test_failure`, `debug_attach_failure`,
`infrastructure_failure`, `stale_handle`, `transport_conflict`, `not_implemented`,
plus new categories — `allocation_denied` (admission/quota), `quota_exceeded`,
`lease_expired`, `provisioning_failure`, `install_failure`, `transport_failure`,
`control_failure`. Pick the most specific value; do not invent strings.

`stale_handle` and `transport_conflict` carry over from the PoC and matter *more*
in the distributed model: stale handles surface after a reprovision or reboot
invalidates a System/DebugSession reference; transport conflicts surface when two
attaches contend for one debug transport.

## Delivery status

This document describes the live architecture. Historical milestone sequencing and exit criteria
remain in the archived plans and designs rather than being repeated as future-tense requirements
here.

| Delivery band | Current status | Historical record |
|---|---|---|
| M0 walking skeleton | Implemented by the server, worker, reconciler, durable stores, and local-libvirt lifecycle | [M0 implementation plan](../archive/plans/m0-implementation.md) |
| M1 platform depth | Implemented allocation, accounting, RBAC, scheduling, live-stack validation, and fault injection | [M1 implementation plan](../archive/plans/m1-implementation.md) |
| M2 provider and operations | Implemented remote-libvirt, deployment packaging, `kdivectl`, observability, managed images, and remote capture paths | [M2 productionization design](../archive/superpowers/specs/2026-06-10-m2x-productionization-band-design.md) |

The provider-runtime hypothesis is now established by local-libvirt, fault-inject, and
remote-libvirt: each provider supplies typed plane ports behind `ProviderRuntime`, while core
lifecycle and MCP contracts remain provider-neutral. The current provider capabilities are listed
in [Provider model](#provider-model) and [Lifecycle planes](#lifecycle-planes).

Cloud, bare-metal, and PowerVM are future provider families. Each should extend the typed runtime
seam unless an accepted ADR establishes a different dispatch model. Hard per-tenant sandboxing and
a manager-backed secret backend also remain future work; their contracts must be decided before
implementation rather than inferred from the completed milestone plans.
