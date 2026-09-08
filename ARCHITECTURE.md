# KDIVE Architecture

This is a one-page summary. The authoritative architecture is
[`docs/design/top-level-design.md`](docs/design/top-level-design.md); read it for
the precise lifecycles, state machines, and decisions. Architecture decisions are
recorded as ADRs under [`docs/adr/`](docs/adr/).

KDIVE gives agentic coding environments a full Linux kernel build → boot → debug
lifecycle as a multi-user MCP service. It is Python 3.14, managed with `uv`.

## Runtime roles

`python -m kdive {server|worker|reconciler|lifecycle-witness}` (`src/kdive/__main__.py`):

- **server** — the FastMCP streamable-HTTP app. Owns the lifecycle state
  machines, authz (OIDC/RBAC with on-behalf-of agent attribution), and admission
  control (quota/budget). It stays thin and fast and never blocks on a long
  provision; long operations are enqueued as jobs and the tool returns
  `{job_id, status: running}` for the agent to poll.
- **worker** — pulls durable jobs from the Postgres-backed queue and runs the
  provider operations (provision, install, boot, capture-vmcore, debug ops).
  Dispatch lanes separate ordinary jobs from state-fenced lifecycle work.
- **reconciler** — a periodic drift-repair loop (ADR-0021): tears down orphaned
  Systems, fails Runs on torn-down Systems, reclaims expired leases, and detaches
  dead DebugSessions.
- **lifecycle-witness** — Kubernetes worker-termination evidence and credential delivery.
  The portable Compose and systemd core uses the first three roles with operator-side gates.

State of record is **Postgres**; bulk artifacts (vmcores, build outputs,
console/gdb transcripts) live in an **S3-compatible object store**, referenced by
row. Postgres advisory locks serialize per-Allocation and per-System work.

## Six durable objects

```
(principal / project) ──< Investigation ──┐
                                          ├──< Run ──< DebugSession
   Resource ──< Allocation ──< System ────┘
```

An Investigation groups experiments across Allocations. A Run can start unbound, holding an
uploaded build for a target kind; binding it to a System establishes its Allocation. Systems
consume leased capacity, while experiment records remain available after VM teardown.
See [domain concepts](docs/guide/concepts.md) for the relationships.

Kernels are compiled by the caller and uploaded to KDIVE. The service validates uploaded
artifacts with `runs.complete_build`, then installs and boots them. See the
[build/upload guide](docs/operating/external-build-upload.md).

## The provider-runtime seam

Providers plug in behind `ProviderRuntime` typed ports (ADR-0063). Production
assembly happens in `providers/assembly/composition.py`, which builds a `ProviderResolver`
over the registered runtimes:

- **local-libvirt** is the default production provider.
- **remote-libvirt** is an operator-configured opt-in, wired through the same
  resolver and runtime seam.
- **fault-inject** is a test/failure-path opt-in provider.

A provider implements the narrow per-plane port protocols for the planes it
supports (Discovery, Provisioning, Install, Connect, Debug, Control,
Retrieve); Allocation is a core plane, not a provider plane. Future provider
families (cloud, bare-metal, PowerVM) follow this path unless a new ADR justifies
broader dispatch.
