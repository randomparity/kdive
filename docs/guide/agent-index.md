# Driving a kdive investigation

This is the entry point for an agent driving the kdive tool surface. It maps the typical
session to the toolsets you call at each stage, then links a per-toolset guide. Each guide
explains what its tools are for and when to reach for them; the exact parameters and return
schema live in each tool's own description.

## Reaching tools

Calling tools **directly by name** (surfaced by lazy-loading hosts as `mcp__kdive__*`) is
the canonical path — by default the server lists its full catalog. If a capability you need
is not a callable tool in your client — including lazy-loading hosts that materialize only
some of the ~100 tools and may never bind `tools.invoke` — reach it through the gateway:
`tools.search` finds the name and schema, and `tools.invoke(name, arguments)` executes any
registered tool. `tools.search` and `tools.invoke` are always available. Both paths enforce
the same RBAC. If an operator enables the core-set gateway, only a small core set is listed
directly, so reach everything else through `tools.search` / `tools.invoke`.

## The typical session

A reproduce-and-investigate session moves through these stages. Each names the toolset and
the first tool to call.

1. **Orient and discover** — start with `session.whoami` to see who you are and which
   projects and roles you hold, then survey what you can provision: `resources.list` and
   `resources.availability` for the registered hosts and their free capacity, `shapes.list`
   for the named VM shapes, and `accounting.estimate` to price a shape × lease-window in KCU
   before you spend. Then `investigations.open` to group the runs of one investigation.
2. **Acquire capacity** — `allocations.request`, then `allocations.wait` until granted.
3. **Define and provision a system** — `images.describe` to pick a base image and check its
   capabilities first. Then take one of two lanes: `systems.provision` directly (profile
   inline, no rootfs-upload window), or `systems.define` followed by
   `systems.provision_defined` (opens a rootfs-upload window between the two calls). See the
   images guide.
4. **Build** — `runs.create` on the external lane, then declare and upload the prebuilt
   kernel with `artifacts.expected_uploads` to see what is required, `artifacts.create_run_upload`
   per artifact to get a presigned PUT URL, and the presigned PUT itself; once every expected
   artifact is uploaded, call `runs.complete_build`. See the runs guide and the build lane
   (resource://kdive/docs/operating/external-build-upload.md).
5. **Install and boot** — `runs.install` then `runs.boot`.
6. **Reproduce in the guest** — `systems.authorize_ssh_key`, then `jobs.wait` until it
   succeeds, then drive the reproducer over SSH (compile in-guest or cross-compile and `scp`,
   then run or stress it). This is where most investigation time goes; see the
   reproduce-and-capture loop below.
7. **Observe evidence** — `runs.get` for status and console access: `refs.latest_console` jumps
   to the newest console artifact, and `include_console_artifacts=true` returns the full
   Run-scoped console manifest (`data.console_artifacts`). Use `artifacts.get` to read an artifact
   and `artifacts.list` (keyset-paginated) for the System's other logs and files.
8. **Debug live** — `debug.start_session`, then breakpoints, memory, and stack tools; or
   `debug.start_session(transport="drgn-live")` followed by `introspect.run`/`introspect.script`
   for non-halting drgn introspection against that session. See the debug and introspect guides.
9. **Triage a crash** — induce one deliberately with `control.force_crash` if needed, then
   `vmcore.fetch` and `postmortem.triage`. See the control and postmortem guides.
10. **Wind down** — release everything you acquired, in order: `systems.teardown` to
    destroy the provisioned guest (a completed teardown does not itself release the
    allocation), then `allocations.release` to return the leased capacity, then
    `investigations.close` to close out the investigation. Release the allocation and tear
    down the system even if you leave the investigation open, so capacity is not held.

Long steps (provision, build, install, boot, capture) return a job handle; poll it with
`jobs.wait`.

## The guest is yours — you have root

Once a system is ready, authorize your public key with `systems.authorize_ssh_key` and poll
`jobs.wait` until it succeeds; only then do you have **root SSH into the guest** — kdive never
holds the private key. From there the guest is yours to shape:

- **The guest package manager is yours.** Install whatever the investigation needs at
  runtime — `apt install trace-cmd`, a compiler toolchain, `stress-ng`, `bpftrace`. Do not
  assume a capability is missing because a tool is absent; install it. Most "the platform
  can't do that" conclusions are one package (or one config symbol) away. On
  **local-libvirt**, installs need the operator to have enabled guest egress first (no
  outbound network by default); see the systems guide's "Reaching the guest over SSH"
  section if `dnf`/`apt install` can't resolve a host.
- **Mind disk headroom.** Installing toolchains, building reproducers, and capturing traces
  all consume guest disk; size the shape for the work or clean up as you go.

## The reproduce-and-capture loop

Most real investigation time is spent here, not in the setup stages. After
`systems.authorize_ssh_key` succeeds (poll `jobs.wait`):

1. **Get the reproducer into the guest.** Compile it in-guest with the toolchain you
   installed, or cross-compile on the host and `scp` the binary in.
2. **Run or stress it** over SSH — the reproducer itself, `stress-ng`, a fuzzer, whatever
   provokes the bug.
3. **Steer the kernel into the failure.** Fault injection (`failslab` / `fail_page_alloc`
   via debugfs, if you built the kernel with `CONFIG_FAULT_INJECTION`), tracing (`ftrace`,
   `bpftrace`), and stress are all in-guest-over-SSH activities using tools you installed
   (see "The guest is yours" above). **To target one allocation site** instead of whatever
   fires first, default to the *bounded* knob: write `1` to `/proc/self/fail-nth` so exactly
   one eligible allocation fails and the injector then disarms — it cannot storm. Scope which
   allocations count as eligible with `cache-filter` (pin it to the exact slab-cache name from
   `/proc/slabinfo`) and boot `slab_nomerge` so SLUB doesn't merge same-size caches out from
   under your filter. To reach a `GFP_KERNEL` site you must also set `ignore-gfp-wait=N` (`Y`,
   the debugfs default, skips every `GFP_KERNEL` allocation before `cache-filter`/`fail-nth`
   even run); with `fail-nth=1` that stays bounded. **Do not reach for `probability` on a
   targeted reproducer:** `probability` together with `ignore-gfp-wait=N` fails `GFP_KERNEL`
   allocations *persistently*, and when one such failure lands in the page-fault path the
   kernel retries `handle_mm_fault` forever — a `VM_FAULT_OOM` retry storm that livelocks the
   guest (nothing detects a livelock; it is worse than a crash). Reserve `probability` for
   stress/soak runs, not surgical single-site reproducers. The old caveat that `fail-nth`
   "trips on the first eligible call, not necessarily the one you're after" only holds when
   you *cannot* scope — `cache-filter` scopes it. This is manual guest-side work today; a
   debugfs-driven fault-injection tool surface may land in a future release.

**A panic drops your SSH channel.** When the kernel crashes, the SSH session dies with it, so
whatever you were watching over SSH is gone. The **serial-console is the durable record** — it
persists across the crash. For a repeat-until-crash race, start `control.watch_for_crash` on the
system first, then run the loop over SSH: it watches the console out-of-band for the crash
signature and returns on the first hit (`fired` with the matched slice + elapsed, or `not_fired`
if none appeared). Poll it with `jobs.wait`. If your SSH loop dies but the watch says `not_fired`,
the crash was outside the watched window — read the console directly with `runs.get` (console
access) and the `artifacts` tools. Do not rely on SSH output as your capture of a panic; rely on
the console.

**Checkpoint a configured guest to restore between attempts.** When each reproducer attempt leaves
the guest dirty (or crashes it), `systems.snapshot` a fully-configured guest once — packages
installed, reproducer staged, kdump armed — then `systems.restore` back to that checkpoint in
seconds between attempts instead of reprovisioning from scratch. `systems.get`'s
`data.supports_snapshots` tells you whether the provider supports this. A memory checkpoint
(`include_memory=true`) resumes the guest exactly where it was; `start_paused=true` lands it
`paused` for a gdbstub `debug.start_session` before execution resumes, then
`control.power(action="resume")` runs it. See the systems guide.

**Scope resource-exhaustion reproducers to a throwaway uid.** Reproducing a per-uid or
per-cgroup quota bug (inotify watches, file descriptors, pending signals, and the like) by
running the workload as root exhausts *root's own* quota — starving root-owned services such as
sshd's session setup and systemd, which hangs new SSH logins and looks exactly like a guest
wedge. Run the reproducer under a throwaway unprivileged uid instead, e.g. `setpriv --reuid
$(id -u nobody) --clear-groups ...`, so the exhaustion is scoped to that uid and your SSH/control
channel stays reachable and recoverable.

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

Live drgn introspection (`introspect.run`/`introspect.script`) needs **no** provisioning knob —
any ready local system can attach (the SSH forward is rendered on every domain), and the only
image requirement is a drgn-capable guest. But it is not provision-free at call time: both tools
take a `session_id` and only resolve against a live **drgn-live** `DebugSession`, so you must
first open one with `debug.start_session(transport="drgn-live")`. The two debug knobs above are
detailed next.

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

Live drgn introspection (`introspect.run`/`introspect.script`) is **not** provision-bound: the
SSH forward is rendered on every domain and the drgn-over-SSH transport authenticates with the
per-System bootstrap key, so a ready local system needs no credential knob. Its only image
requirement is a drgn-capable guest (`introspect.run` reports `missing_dependency` if drgn is
absent). It does, however, require a live session: call
`debug.start_session(transport="drgn-live")` first and pass the returned `session_id` to
`introspect.run`/`introspect.script` — a successful drgn-live attach suggests both as next
actions. Use `debug.end_session` to release the session when you're done.

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
