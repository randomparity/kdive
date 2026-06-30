# Driving a kdive investigation

This is the entry point for an agent driving the kdive tool surface. It maps the typical
session to the toolsets you call at each stage, then links a per-toolset guide. Each guide
explains what its tools are for and when to reach for them; the exact parameters and return
schema live in each tool's own description.

## The typical session

A reproduce-and-investigate session moves through these stages. Each names the toolset and
the first tool to call.

1. **Orient** — `investigations.open` to group the runs of one investigation.
2. **Acquire capacity** — `allocations.request`, then `allocations.wait` until granted.
3. **Define and provision a system** — `systems.define`, then `systems.provision`.
4. **Build** — upload a prebuilt kernel (`runs.create` on the default external lane) or
   build on a host. See the runs guide.
5. **Install and boot** — `runs.install` then `runs.boot`, or `runs.build_install_boot` as
   one pollable job on the single-host server-build lane.
6. **Observe evidence** — `runs.get` for status and console access, `artifacts.list` and
   `artifacts.get` for logs and other files.
7. **Debug live** — `debug.start_session`, then breakpoints, memory, and stack tools.
8. **Triage a crash** — `vmcore.fetch`, then `postmortem.triage`.
9. **Release** — `allocations.release` when done.

Long steps (provision, build, install, boot, capture) return a job handle; poll it with
`jobs.wait`.

## Toolset guides

| Toolset | What it is for | Guide |
|---|---|---|
| runs | Build, install, and boot lifecycle of a kernel test run | resource://kdive/docs/guide/toolsets/runs.md |
| artifacts | Fetch run evidence (logs, console, vmlinux) and upload builds | resource://kdive/docs/guide/toolsets/artifacts.md |
| debug | Live GDB kernel debugging — breakpoints, memory, registers, stacks | resource://kdive/docs/guide/toolsets/debug.md |
| systems | Provision, reprovision, and reach the target system over SSH | resource://kdive/docs/guide/toolsets/systems.md |

For the shape every tool result returns, read
resource://kdive/docs/guide/response-envelope.md. Clients that list MCP prompts also have
the `start_investigation`, `build_boot_debug`, and `triage_panic` lifecycle prompts.
