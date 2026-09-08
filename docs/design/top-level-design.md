# KDIVE current architecture

## Purpose

KDIVE is a production, multi-user service that gives agentic coding environments
(Claude Code, Codex) a complete Linux kernel development and debug lifecycle.
Local VMs are the default provider. Remote libvirt is an operator-configured
provider; remote bare metal, PowerVM, and cloud providers remain future work.

KDIVE was implemented as a greenfield Python rewrite of a single-user, local,
stdio proof of concept. Python provides native access to the kernel-tooling
ecosystem (drgn, libvirt bindings, crash, and the MCP SDK).

This page explains the architecture implemented by this checkout. Start with the
[overview](../../ARCHITECTURE.md) for orientation and [domain concepts](../guide/concepts.md)
for the reader-facing object model. The [tool reference](../guide/reference/index.md) owns exact
MCP contracts; [ADRs](../adr/README.md) record decisions and subsequent amendments.

## Core decisions

- **MCP over streamable HTTP** serves multiple projects and principals with OIDC and RBAC.
- **Postgres** owns durable state, admission accounting, jobs, and audit records.
  An **S3-compatible object store** owns bulk artifacts.
- **Separate runtime roles** keep request handling independent of provider operations and
  reconciliation. Kubernetes adds a lifecycle witness for worker identity and termination.
- **Typed provider ports** keep local-libvirt, remote-libvirt, and the opt-in fault-inject
  provider behind a common runtime seam.
- **External kernel builds** keep compilation in the caller's environment. KDIVE consumes
  uploaded artifacts through `runs.complete_build`, then installs and boots them
  ([ADR-0316](../adr/0316-remove-server-build-lane.md)).

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
        │  (provision/install/boot  │    │  resources, allocations,     │
        │   /debug-op/control jobs) │    │  systems, investigations,    │
        └───────────┬──────────────┘    │  runs, reservations,         │
                    ▼                    │  accounting ledger, audit    │
        ┌──────────────────────────┐    └──────────────────────────────┘
        │   Generic worker fleet    │    ┌──────────────────────────────┐
        │  run provider operations  │───▶│  Object store (S3-compatible)│
        │  operation dispatch lanes │    │  vmcores, build outputs,     │
        └───────────┬──────────────┘    │  console/gdb transcripts     │
                    ▼                    └──────────────────────────────┘
   providers: local-libvirt │ fault-inject (test opt-in) │ remote-libvirt (configured)

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

[Domain concepts](../guide/concepts.md) defines Resource, Allocation, System, Investigation,
Run, and DebugSession and their relationships. An Investigation groups experiment history;
Allocations lease capacity; Systems consume that capacity. A Run can hold an uploaded build
before it is bound to a System. Binding establishes its Allocation through the System.

The [state definitions and transition table](../../src/kdive/domain/capacity/state.py) are the
implementation contract. Repository writes enforce legal transitions. Keep detailed state
lists there rather than reproducing them in this overview.

The pure domain layer owns models and rules. [Services](../../src/kdive/services/) compose
transactions, repository calls, advisory locks, audit records, and admission accounting.
A live System cannot outlast its Allocation; teardown retains experiment records for later
inspection. Run build status and install/boot progress are separate contracts, as described
in the [Run reference](../guide/reference/runs.md).

## Provider model

Providers are the extension seam.

### Current status

The production seam is
[`ProviderRuntime`](../../src/kdive/providers/core/runtime.py): startup builds typed ports for each configured provider
(`Provisioner`, `Installer`, `Booter`, `Controller`, `Retriever`, debug and
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
destructive-op gating, or reconciler behavior. ADR-0063 records this narrowing
and ADR-0066 removed the prototype source so contributors extend the runtime that actually
serves requests.

## Lifecycle planes

| Plane | Current responsibility |
|---|---|
| Discovery | Discover local hosts and register configured remote-libvirt resources and capabilities |
| Allocation | Core capacity, lease, budget, and quota admission for either provider |
| Provisioning | Create and customize a libvirt domain and rootfs |
| Build input | Accept and validate caller-built kernel artifacts; kernel compilation is external |
| Install and boot | Stage validated artifacts and boot the target through provider ports |
| Connect and debug | Establish GDB, SSH, and console access; inspect live state with GDB or drgn |
| Control | Power, reset, policy-gated force-crash, and supported diagnostic operations |
| Retrieve and postmortem | Capture vmcores, retrieve artifacts, and analyze dumps with drgn or crash |

Provider-advertised capabilities determine supported operations. See
[platform support](../operating/platform-support.md) and the
[local](../operating/providers/local-libvirt.md) and
[remote](../operating/providers/remote-libvirt.md) provider references for operational requirements.

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

Provider lifecycle semantics, provider filenames, S3 upload mechanics, and MCP response shaping stay
outside these data-owner packages unless the package above names that responsibility.

Historical build-config catalog designs live under `docs/archive/design/`. They were superseded
by [ADR-0316](../adr/0316-remove-server-build-lane.md); `kdive.build_configs`, `buildconfig.*`,
and the build-config catalog are not part of the live architecture.

## MCP tool surface

Tools return a [`ToolResponse`](../../src/kdive/mcp/responses.py) with object identity, status,
next actions, artifact references, and categorized failures. See the
[response envelope](../guide/response-envelope.md) for the wire contract.

Provisioning, install, boot, and capture operations enqueue durable jobs. Use `jobs.wait`
with the returned job id to follow their outcomes; a Run's build status alone cannot establish
boot success. Allocation admission instead returns an allocation state and uses
`allocations.wait` when queued. The [async-job guide](../guide/async-jobs.md) owns these patterns.

The [core workflow](../guide/core-path.md) shows the user journey and the
[generated tool reference](../guide/reference/index.md) lists exact names and parameters.
Tool wrapper docstrings and parameter descriptions are the agent-visible contracts; update
those sources when behavior changes, then regenerate the reference.

### Where to change the code

| Concern | Entry point |
|---|---|
| Runtime roles | [`__main__.py`](../../src/kdive/__main__.py) |
| MCP application assembly | [`mcp/assembly/app.py`](../../src/kdive/mcp/assembly/app.py) |
| Tool registration | [`mcp/assembly/tool_registration.py`](../../src/kdive/mcp/assembly/tool_registration.py) |
| Worker handler registration | [`jobs/assembly.py`](../../src/kdive/jobs/assembly.py) |
| Provider composition | [`providers/assembly/composition.py`](../../src/kdive/providers/assembly/composition.py) |
| Drift repair | [`reconciler/loop.py`](../../src/kdive/reconciler/loop.py) |
| Packaged MCP documentation | [`mcp/resources/registrar.py`](../../src/kdive/mcp/resources/registrar.py) |

## Cross-cutting concerns

Applied across every plane.

- **Secrets by reference** — provider credentials are resolved at the worker boundary;
  only `(present, source-ref)` is persisted. When a worker resolves a reference,
  it **registers the resolved value into the process-owned redaction registry**
  passed through runtime composition (ADR-0327) for the op's lifetime, so any transcript or
  console output capturing the value is masked by **exact-value replacement**, not
  merely by the redactor's secret-name patterns. Output captured before
  registration completes is quarantined (object-store, sensitive) until redacted.
- **Mandatory redaction** — guest output, debugger transcripts, and console
  logs pass through the redactor before persistence and before any response
  snippet. Raw artifacts stay in the object store, marked sensitive, fetched only
  through the authorized raw-artifact retrieval path (`artifacts.fetch_raw`).
- **Audit log** — every state transition and every destructive op writes an
  append-only audit row attributing `(principal, agent_session, tool,
  args-digest)`.
- **Accounting ledger** — allocation transitions emit usage events; admission
  control checks budget/quota on `allocations.request` and denies
  requests that exceed them. The budget/quota **check and the resulting ledger debit
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

The reconciler repairs expired leases, orphaned provider resources, affected Runs, and dead
debug sessions. Postgres advisory-lock release is not proof that provider work has stopped.
State-fenced lifecycle work uses durable worker identity and termination evidence before
recovery can release ownership or delete protected artifacts.

Deployment-specific termination and upgrade procedures live in the
[installation guide](../operating/install.md) and its linked runbooks. Cancellation and failure
recovery depend on the operation and its durable state; follow the tool's categorized error,
next actions, and recovery contract instead of treating every failed job as safe to retry.

## Error taxonomy

[`ErrorCategory`](../../src/kdive/domain/errors.py) is the closed error vocabulary. Choose the
most specific existing category when implementing a tool or provider operation. The
[error guide](../guide/errors.md) explains recovery; individual tool contracts state
operation-specific constraints. Do not infer a new error string from a historical design.

## Delivery status

Local-libvirt and configured remote-libvirt are implemented production providers; fault-inject
is an opt-in test provider. [Platform support](../operating/platform-support.md) distinguishes
host/guest architectures and verified or incomplete paths. Cloud, bare-metal, PowerVM, hard
per-tenant sandboxing, and a manager-backed secret backend remain future work.

Historical milestone plans and dated designs record delivery history. They do not describe
the current backlog or authorize a new provider contract. See the
[documentation index](../README.md#current-guidance-and-historical-records) for the reading order.
