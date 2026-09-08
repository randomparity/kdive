# KDIVE agent guide

KDIVE manages kernel experiments on local and remote libvirt VMs. Build the kernel in your
own environment, upload the artifacts, and use MCP tools to provision, install, boot, debug,
and retrieve crash evidence. Cloud, bare-metal, and PowerVM providers remain future work.

Start by [connecting a client](agents/index.md), then read the [domain concepts](concepts.md)
and follow the [core reproduce/verify path](core-path.md). If you need to install the service,
start with the [operating guide](../operating/index.md).

An agent drives KDIVE by calling tools and reading the structured response
envelope each tool returns. Every tool returns a [`ToolResponse`](response-envelope.md)
carrying an `object_id`, a `status`, and a `suggested_next_actions` list of literal
next tool names. That list tells the agent what to call next without inferring it.
When a tool starts a long-running operation — provisioning, building, installing, or
capturing a vmcore — it returns immediately with a job handle (`status: running`) and
the agent polls `jobs.wait` until the job reaches a terminal state.
See [async jobs](async-jobs.md) for the full pattern.

The domain objects separate leased VM capacity from the investigation and its experiment
history. A Run may be created before a System is available and bound later. See
[concepts](concepts.md) for the relationships and lifetime rules.

Destructive operations are protected by explicit policy: `control.force_crash`
uses the destructive-op gate (`admin` role plus provisioning-profile opt-in),
while `systems.teardown` requires the `admin` role directly. `control.power`
and `systems.reprovision` are contributor leaseholder lifecycle over the
caller's own allocation, not destructive-gate operations. See [safety and
RBAC](safety-and-rbac.md).

When a tool reports a failure, the `error_category` field carries a stable string
from a closed taxonomy. See [errors](errors.md).

The [tool reference](reference/index.md) is generated from tool registration and carries
maturity and parameter contracts. Provider capabilities and host prerequisites still determine
which operations your deployment can execute; see [platform support](../operating/platform-support.md).

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
