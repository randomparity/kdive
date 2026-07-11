# KDIVE agent guide

KDIVE is a multi-user service that gives agentic coding environments a complete
Linux kernel development and debug lifecycle across heterogeneous resources: local
VMs, remote libvirt hosts, bare metal (PXE/SoL/IPMI/Redfish), PowerVM LPARs, and
cloud instances. The build→boot→debug premise is that a single service owns the
full chain — claim a resource, provision a system, build and install a kernel,
boot it, attach a debugger, crash it, and retrieve the vmcore — all through one
uniform MCP tool surface.

An agent drives KDIVE by calling tools and reading the structured response
envelope each tool returns. Every tool returns a [`ToolResponse`](response-envelope.md)
carrying an `object_id`, a `status`, and a `suggested_next_actions` list of literal
next tool names. That list tells the agent what to call next without inferring it.
When a tool starts a long-running operation — provisioning, building, installing, or
capturing a vmcore — it returns immediately with a job handle (`status: running`) and
the agent polls `jobs.get` or `jobs.wait` until the job reaches a terminal state.
See [async jobs](async-jobs.md) for the full pattern.

The six domain objects (Resource, Allocation, System, Investigation, Run,
DebugSession) have independent lifecycles but a fixed nesting order. Understanding
that nesting — and knowing that a lower layer outlives its dependents — is the
foundation for driving the tools correctly. See [concepts](concepts.md).

Destructive operations are protected by explicit policy: `control.force_crash`
uses the destructive-op gate (`admin` role plus provisioning-profile opt-in),
while `systems.teardown` requires the `admin` role directly. `control.power`
and `systems.reprovision` are contributor leaseholder lifecycle over the
caller's own allocation, not destructive-gate operations. See [safety and
RBAC](safety-and-rbac.md).

When a tool reports a failure, the `error_category` field carries a stable string
from a closed taxonomy. See [errors](errors.md).

Each tool carries a maturity marker (`implemented`, `partial`, or `planned`). The
allocation, investigation, run-create, and jobs plumbing is `implemented`, but
several provider paths — build → boot → crash → introspect — are `partial` and
live-gated, so they need real infrastructure rather than a stock host. Check the
maturity badges in the [tool reference](reference/index.md) before relying on a
given tool.

## Contents

| Page | What it covers |
|---|---|
| [Core reproduce/verify path](core-path.md) | The ~12-tool path from acquiring capacity to triaging a crash, and the curated MCP prompts |
| [Concepts](concepts.md) | The six durable objects and their lifecycle ordering |
| [Response envelope](response-envelope.md) | `ToolResponse` fields; the references-not-dumps rule |
| [Async jobs](async-jobs.md) | The long-op pattern and the `jobs.*` polling tools |
| [Safety and RBAC](safety-and-rbac.md) | Roles, the destructive-op gate, secrets, and redaction |
| [Errors](errors.md) | The `ErrorCategory` taxonomy and how to recover |
| [Tool reference](reference/index.md) | Generated per-namespace parameter reference |
| [Agent onboarding](agents/index.md) | Wiring an MCP client to KDIVE (config examples, first-call smoke sequence) |

For the full design rationale see [`docs/design/top-level-design.md`](../design/top-level-design.md).
