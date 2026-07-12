# Driving a kdive investigation

This is the entry point for an agent driving the kdive tool surface. It maps the typical
session to the toolsets you call at each stage, then links a per-toolset guide. Each guide
explains what its tools are for and when to reach for them; the exact parameters and return
schema live in each tool's own description.

## Reaching tools

Calling tools **directly by name** (surfaced by lazy-loading hosts as `mcp__kdive__*`) is
the canonical path — by default the server lists its full catalog. `tools.search` and
`tools.invoke` are a discovery gateway for hosts without lazy tool loading; prefer direct
calls when your host lists the tools. Both paths enforce the same RBAC. If an operator
enables the core-set gateway, only a small core set is listed directly, so reach everything
else through `tools.search` / `tools.invoke`.

## The typical session

A reproduce-and-investigate session moves through these stages. Each names the toolset and
the first tool to call.

1. **Orient** — `investigations.open` to group the runs of one investigation.
2. **Acquire capacity** — `allocations.request`, then `allocations.wait` until granted.
3. **Define and provision a system** — `images.describe` to pick a base image and check its
   capabilities first, then `systems.define` and `systems.provision`. See the images guide.
4. **Build** — upload a prebuilt kernel with `runs.create` on the external lane, then
   `runs.complete_build`. See the runs guide.
5. **Install and boot** — `runs.install` then `runs.boot`.
6. **Reproduce in the guest** — `systems.authorize_ssh_key`, then drive the reproducer over
   SSH (compile in-guest or cross-compile and `scp`, then run or stress it). This is where
   most investigation time goes; see the reproduce-and-capture loop below.
7. **Observe evidence** — `runs.get` for status and console access, `artifacts.list` and
   `artifacts.get` for logs and other files.
8. **Debug live** — `debug.start_session`, then breakpoints, memory, and stack tools; or
   `introspect.run` for non-halting drgn introspection. See the debug and introspect guides.
9. **Triage a crash** — induce one deliberately with `control.force_crash` if needed, then
   `vmcore.fetch` and `postmortem.triage`. See the control and postmortem guides.
10. **Release** — `allocations.release` when done.

Long steps (provision, build, install, boot, capture) return a job handle; poll it with
`jobs.wait`.

## The guest is yours — you have root

Once a system is ready, authorize your public key with `systems.authorize_ssh_key` and you
have **root SSH into the guest** — kdive never holds the private key. From there the guest is
yours to shape:

- **The guest package manager is yours.** Install whatever the investigation needs at
  runtime — `apt install trace-cmd`, a compiler toolchain, `stress-ng`, `bpftrace`. Do not
  assume a capability is missing because a tool is absent; install it. Most "the platform
  can't do that" conclusions are one package (or one config symbol) away. On
  **local-libvirt**, installs need the operator to have enabled guest egress first (no
  outbound network by default); see the systems guide's "Reaching the guest over SSH"
  section if `dnf`/`apt install` can't resolve a host.
- **Mind disk headroom.** Installing toolchains, building reproducers, and capturing traces
  all consume guest disk; size the shape for the work or clean up as you go.
- **Mind disk headroom.** Installing toolchains, building reproducers, and capturing traces
  all consume guest disk; size the shape for the work or clean up as you go.

## The reproduce-and-capture loop

Most real investigation time is spent here, not in the setup stages. After
`systems.authorize_ssh_key`:

1. **Get the reproducer into the guest.** Compile it in-guest with the toolchain you
   installed, or cross-compile on the host and `scp` the binary in.
2. **Run or stress it** over SSH — the reproducer itself, `stress-ng`, a fuzzer, whatever
   provokes the bug.
3. **Steer the kernel into the failure.** Fault injection (`failslab` / `fail_page_alloc`
   via debugfs, if you built the kernel with `CONFIG_FAULT_INJECTION`), tracing (`ftrace`,
   `bpftrace`), and stress are all in-guest-over-SSH activities using tools you installed
   (see "The guest is yours" above). **To target one allocation site** instead of whatever
   fires first: set `ignore-gfp-wait=N` (`Y`, the debugfs default, skips every `GFP_KERNEL`
   allocation before `cache-filter`/`fail-nth` even run), pin `cache-filter` to the exact
   slab-cache name from `/proc/slabinfo`, and boot with `slab_nomerge` so SLUB doesn't
   merge same-size caches out from under your filter. Prefer `probability` over
   `/proc/self/fail-nth` when more than one site can fire — `fail-nth` is global and trips
   on the first eligible call in the process (e.g. `fail_usercopy`'s
   `strncpy_from_user`), not necessarily the one you're after. This is manual guest-side
   work today; #918 and #919 track a debugfs-driven fault-injection tool surface.

**A panic drops your SSH channel.** When the kernel crashes, the SSH session dies with it, so
whatever you were watching over SSH is gone. The **serial-console sidecar is the durable
record** — read it with `runs.get` (console access) and the `artifacts` tools, which persist
across the crash. Do not rely on SSH output as your capture of a panic; rely on the console
artifacts.

## Decide before you provision

Several choices are bound at `systems.provision` and expensive to change — altering any of them
means `systems.reprovision`, which rebuilds and reboots the system. Run down this list before
your first provision so every irreversible choice is made up front:

- **Base image** — pick it with `images.describe` and check its `kdump`, `direct_kernel`, and
  `live_drgn` `capability_signals` first (see the images guide). A wrong image can burn the
  allocation. An
  `unverified` signal is normal for an externally-baked or operator-staged image no one has
  characterized — not a defect; the check becomes actionable once the image is published or the
  operator attests it (`basis` then reads `build_verified` or `operator_attested`).
- **Shape and disk** — size vCPUs, memory, and disk for the work; toolchains, reproducer
  builds, and captures all consume guest disk (see "The guest is yours").
- **Kernel config** — the config is baked into the kernel you build and upload; enable the
  debug options you need (KASAN / KCSAN / FAULT_INJECTION / …) before uploading (see the
  external-build-upload doc).
- **`debug.gdbstub: true`** — set it if you may want a live GDB session; without it
  `debug.start_session` fails.
- **`debug.preserve_on_crash: true`** — set it to hold a crashed guest (vCPUs stopped) for
  post-panic inspection.

Live drgn introspection (`introspect.run`) needs **no** provisioning knob — it works on any
ready local system (the SSH forward is rendered on every domain), and its only requirement is a
drgn-capable guest image. The two debug knobs are detailed next.

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

Live drgn introspection (`introspect.run`) is **not** provision-bound: the SSH forward is
rendered on every domain and the drgn-over-SSH transport authenticates with the per-System
bootstrap key, so a ready local system needs no credential knob. Its only requirement is a
drgn-capable guest image (`introspect.run` reports `missing_dependency` if drgn is absent).

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
| images | Pick a base image and read its capabilities before provisioning | resource://kdive/docs/guide/toolsets/images.md |
| introspect | Non-halting drgn introspection of a live guest or a captured vmcore | resource://kdive/docs/guide/toolsets/introspect.md |
| control | Deliberately induce a crash, send a diagnostic SysRq, drive power | resource://kdive/docs/guide/toolsets/control.md |
| postmortem | Capture a crashed kernel's vmcore, then triage or analyze it | resource://kdive/docs/guide/toolsets/postmortem.md |

For the shape every tool result returns, read
resource://kdive/docs/guide/response-envelope.md. Clients that list MCP prompts also have
the `start_investigation`, `build_boot_debug`, and `triage_panic` lifecycle prompts.
