# Core reproduce/verify path

The MCP tool surface is large — roughly 150 tools across ~30 namespaces — but
ops, admin, and accounting tooling dominate it. An agent doing a
basic *reproduce a crash, then verify it* task needs only about a dozen of them.
This page lists that core path in order so you do not have to wade through the
full [tool reference](reference/index.md) to find the sequence.

The same sequence is also surfaced at runtime as a set of curated MCP prompts —
see [Curated MCP prompts](#curated-mcp-prompts) below — so you can pull it from
the prompt surface or from here.

## The twelve-step path

The path has three phases: acquire capacity, upload and boot the kernel, then
crash it and verify the result.

### Acquire capacity

| Step | Tool | Purpose |
|---|---|---|
| 1 | `investigations.open` | Open an investigation to group related runs. |
| 2 | `allocations.request` | Request capacity on a resource. |
| 3 | `allocations.wait` | Wait until the allocation is granted. |
| 4 | `systems.define` | Define the target system to build and boot on. |

### Upload and boot

You build the kernel locally and upload the artifacts — the platform never compiles
kernel source. See [Build lane](../operating/external-build-upload.md) for the artifact recipe.

| Step | Tool | Purpose |
|---|---|---|
| 5 | `runs.create` | Create a run against the system and build target. |
| 6 | `artifacts.create_run_upload` | Declare and upload the kernel you built locally. |
| 7 | `runs.complete_build` | Validate the uploaded artifacts and record the build outputs. |
| 8 | `runs.install` | Install the built kernel onto the system. |
| 9 | `runs.boot` | Boot the system into the built kernel. |

### Crash and verify

| Step | Tool | Purpose |
|---|---|---|
| 10 | `control.force_crash` | Induce a crash (or react to an observed panic). |
| 11 | `vmcore.fetch` | Capture the vmcore from the crashed system. |
| 12 | `postmortem.triage` | Run the first-pass crash triage. |

`runs.install`, `runs.boot`, `control.force_crash`, and
`vmcore.fetch` are long-running: each returns a job handle and you poll
`jobs.wait` or `jobs.get` until it reaches a terminal state. See
[async jobs](async-jobs.md). At each step, prefer the `suggested_next_actions`
field in the [response envelope](response-envelope.md) over re-deriving the next
call from this list.

## Going deeper

The core path stops at first-pass triage. Two common extensions:

- **Live debug** — instead of (or before) forcing a crash, attach a live
  session with `debug.start_session`, inspect with `introspect.run`, then
  `debug.end_session`.
- **Inspect the captured core** — after `vmcore.fetch`, confirm the reference
  with `vmcore.list` and inspect kernel state with `introspect.from_vmcore`.

`control.force_crash` uses the destructive-op gate (`admin` role plus profile
opt-in); `systems.teardown` requires the `admin` role. `control.power` and
`systems.reprovision` are contributor leaseholder lifecycle over a READY
transient VM (not gated). See [safety and RBAC](safety-and-rbac.md).

## Curated MCP prompts

KDIVE registers this path as three ordered MCP prompts ([ADR-0202](../adr/0202-mcp-lifecycle-prompts.md)),
discoverable through `ListMcpPrompts` on the server. Each is a thin pointer into
the real tools — an ordered list of registered tool names with a one-line
purpose per step — and each maps to a phase above:

| Prompt | Covers | Steps above |
|---|---|---|
| `start_investigation` | Orient and acquire capacity | 1–4 (plus `resources.list`) |
| `build_boot_debug` | Build, boot, and live-debug a kernel | 5–9 (plus the live-debug steps) |
| `triage_panic` | Turn a crash into a vmcore and a postmortem | 10–12 (plus `vmcore.list` / `introspect.from_vmcore`) |

The prompts carry maturity disclosure: a step backed by a `partial` tool is
tagged with its maturity reason rather than dropped, so a prompt never silently
steers you into a not-yet-proven tool. Check a tool's maturity badge in the
[tool reference](reference/index.md) before relying on it.
