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

## The guest is yours — you have root

Once a system is ready, authorize your public key with `systems.authorize_ssh_key` and you
have **root SSH into the guest** — kdive never holds the private key. From there the guest is
yours to shape:

- **The guest package manager is yours.** Install whatever the investigation needs at
  runtime — `apt install trace-cmd`, a compiler toolchain, `stress-ng`, `bpftrace`. Do not
  assume a capability is missing because a tool is absent; install it. Most "the platform
  can't do that" conclusions are one package (or one config symbol) away.
- **Mind disk headroom.** Installing toolchains, building reproducers, and capturing traces
  all consume guest disk; size the shape for the work or clean up as you go.

## Provisioning for debugging and live introspection

Some debugging and live-introspection capabilities are bound at `systems.provision` and
**cannot be turned on afterward** — a ready system has no knob to flip. If you decide to
debug only after the run boots, the only remedy is `systems.reprovision`, which rebuilds
and reboots the system (an expensive cycle). Decide these before you provision:

- `provider.local-libvirt.debug.gdbstub: true` — provisions the QEMU gdb stub a live GDB
  session attaches to. Without it, `debug.start_session` fails and you must reprovision.
- `provider.local-libvirt.debug.preserve_on_crash: true` — holds a crashed guest (vCPUs
  stopped) instead of destroying it, so you can attach and inspect the halted kernel after
  a panic.
- `provider.local-libvirt.ssh_credential_ref` — the guest credential the drgn-over-SSH
  live-introspection transport (`introspect.run`) resolves to reach the guest. It is
  necessary but not sufficient: live introspection also needs a drgn-capable guest image
  and a guest that is reachable over SSH.

These flags default off, so a plain profile provisions a system you can build, boot, and
observe on but not live-debug. `systems.profile_examples` returns starting-point profiles;
add the `debug` section and credential above before provisioning if the investigation
needs them.

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
