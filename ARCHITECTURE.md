# KDIVE Architecture

This is a one-page summary. The authoritative architecture is
[`docs/design/top-level-design.md`](docs/design/top-level-design.md); read it for
the precise lifecycles, state machines, and decisions. Architecture decisions are
recorded as ADRs under [`docs/adr/`](docs/adr/).

KDIVE gives agentic coding environments a full Linux kernel build → boot → debug
lifecycle as a multi-user MCP service. It is Python 3.14, managed with `uv`.

## Three processes, one codebase

`python -m kdive {server|worker|reconciler}` (`src/kdive/__main__.py`):

- **server** — the FastMCP streamable-HTTP app. Owns the lifecycle state
  machines, authz (OIDC/RBAC with on-behalf-of agent attribution), and admission
  control (quota/budget). It stays thin and fast and never blocks on a long
  provision; long operations are enqueued as jobs and the tool returns
  `{job_id, status: running}` for the agent to poll.
- **worker** — pulls durable jobs from the Postgres-backed queue and runs the
  provider operations (provision, build, install, capture-vmcore, debug ops).
  Worker pools are scoped per resource class.
- **reconciler** — a periodic drift-repair loop (ADR-0021): tears down orphaned
  Systems, fails Runs on torn-down Systems, reclaims expired leases, and detaches
  dead DebugSessions.

State of record is **Postgres**; bulk artifacts (vmcores, build outputs,
console/gdb transcripts) live in an **S3-compatible object store**, referenced by
row. Postgres advisory locks serialize per-Allocation and per-System work.

## Six durable objects

```
(principal / project) ──< Investigation ──┐
                                          ├──< Run ──< DebugSession
   Resource ──< Allocation ──< System ────┘
```

Within the `Resource → Allocation → System → Run → DebugSession` chain, lower
layers outlive higher ones — a System never outlives its Allocation. The
`Investigation` is cross-cutting: it groups Runs across Allocations and resource
kinds, and its lifetime is independent of any single Allocation. A Run is the
join point, belonging to exactly one System (which fixes its Allocation) and
exactly one Investigation. Each object is a Postgres row with an explicit state
machine; the design doc's "Domain model" section gives the per-object lifecycles.

## The provider-runtime seam

Providers plug in behind `ProviderRuntime` typed ports (ADR-0063). Production
assembly happens in `providers/assembly/composition.py`, which builds a `ProviderResolver`
over the registered runtimes:

- **local-libvirt** is the default production provider.
- **remote-libvirt** is an operator-configured opt-in, wired through the same
  resolver and runtime seam.
- **fault-inject** is a test/failure-path opt-in provider.

A provider implements the narrow per-plane port protocols for the planes it
supports (Discovery, Provisioning, Build, Install, Connect, Debug, Control,
Retrieve); Allocation is a core plane, not a provider plane. Future provider
families (cloud, bare-metal, PowerVM) follow this path unless a new ADR justifies
broader dispatch.
