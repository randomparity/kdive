# Debugging a kernel race

Race investigation — a data race, a use-after-free window, a lost wakeup — needs you to
**watch a live kernel value without stopping it**. A halting debugger changes the timing that
produces the bug: the moment you break at a GDB stub, every CPU freezes, the race window
closes, and the failure stops reproducing. This guide routes race work to the tools that
observe a running kernel **without halting it**, and documents the contract that makes those
tools reachable: once a system is ready, the guest is yours as root over SSH.

This is the resolution of **#986** (a non-halting observation mode). kdive does **not** ship a
non-stop gdbstub mode; the existing out-of-band primitives — drgn-live introspection and
in-guest tracepoints — already answer "observe a kernel value under race load without
halting the VM," and the reproducer loop that provokes the race is your own code run over root
SSH. See [ADR-0366](../adr/0366-race-debugging-out-of-band.md) for that decision.

## Why not a gdbstub break

The `debug` toolset attaches a GDB session to the guest's stub and can set breakpoints and
hardware watchpoints — but hitting one **halts every CPU** (see the
[debug toolset guide](../guide/toolsets/debug.md)). That is correct for a deterministic bug
you can stop at and inspect. It is the wrong tool for a race: halting collapses the concurrent
timing you are trying to observe, so the value you wanted to catch mid-flight never occurs
under the debugger. Reach for the `debug` stub when you want to **stop and step**; reach for
the tools below when you need to **watch without stopping**.

## Route 1 — drgn-live: non-halting kernel introspection

`drgn` reads a running kernel's memory and walks typed kernel structures **without stopping
the CPU**, so it is the race-friendly way to inspect live state. It is the `introspect`
toolset (see the [introspect guide](../guide/toolsets/introspect.md)):

- `introspect.run` — run an in-tree helper (`tasks`, `modules`, `sysinfo`) against a live
  drgn-live session. Start here for common questions.
- `introspect.script` — run your own drgn script against a live session. This is the
  supported way to read a **struct field or array member by name** on a live guest
  (`prog["some_struct"].field[3].member`) — drgn resolves typed kernel objects that the
  halting gdbstub path (which yields only an address) cannot.

Live introspection reaches the guest over the drgn-over-SSH transport and needs **no**
credential provisioning: the SSH forward is rendered on every domain and the transport
authenticates with the per-System bootstrap key, so any ready local system qualifies. The
only requirement is a drgn-capable guest image. Start the session with
`debug.start_session(transport="drgn-live")`, then call `introspect.run` / `introspect.script`
against it. To sample a value repeatedly while the race is under load, run the reproducer (Route 3)
and poll `introspect.script` in a loop — each read is non-halting, so the timing you are
studying is preserved.

Unlike a gdbstub break, drgn-live does **not** require the system to have been provisioned with
`debug.gdbstub` — no `nokaslr`, no reprovision. Any ready system with a drgn-capable image
qualifies.

## Route 2 — tracepoints and ftrace over root SSH

The kernel's own tracing infrastructure — static tracepoints, function tracing, kprobes,
`bpftrace` — records events as they happen with low overhead and **no halt**. You drive it
in-guest over SSH through `/sys/kernel/tracing` (and `/sys/kernel/debug/tracing` on older
guests):

- Enable a tracepoint or an ftrace function-graph over SSH, run the reproducer, then read
  `trace` / `trace_pipe`.
- Use `trace-cmd` or `bpftrace` for anything beyond raw sysfs writes.
- Fault injection (`failslab` / `fail_page_alloc` via debugfs, with a `CONFIG_FAULT_INJECTION`
  kernel) steers the kernel into the failure window — see the
  [reproduce-and-capture loop](../guide/agent-index.md#the-reproduce-and-capture-loop) in the
  agent index for the debugfs knobs (`ignore-gfp-wait`, `cache-filter`, `probability` vs
  `fail-nth`, `slab_nomerge`).

None of this is an MCP tool, and it does not need to be — it runs entirely inside a guest you
own. That is the point of the next section.

## The contract: the guest is yours as root

Once a system is ready, authorize your public key with `systems.authorize_ssh_key` and poll
`jobs.wait` until it succeeds; only then do you have **root SSH into the guest** — kdive never
holds the private key. From there the guest is yours to shape (see
[The guest is yours](../guide/agent-index.md#the-guest-is-yours--you-have-root) in the agent
index and the [systems guide](../guide/toolsets/systems.md#reaching-the-guest-over-ssh)):

- **The guest package manager is yours.** Install whatever the investigation needs at runtime
  — `apt install trace-cmd`, `bpftrace`, a compiler toolchain, `stress-ng`. Do not conclude a
  capability is missing because a tool is absent; install it.
- **Run commands and loop reproducers over SSH.** Compiling a reproducer, stressing it,
  enabling a tracepoint, sampling `/proc`, or reading a drgn value on a loop are all
  guest-side actions you drive over your own SSH channel — they need no dedicated tool.

This contract is why race investigation needs no new MCP tools: every outcome the discarded
SSH-equivalent tool proposals (the closed #998 set) would have provided is reachable as a
prompt pattern over root SSH. A capability earns an MCP tool **only if it is out-of-band** —
only if it works when SSH cannot reach the guest.

On **local-libvirt** the guest has **no outbound egress by default**, so runtime `dnf`/`apt
install` fails to resolve any mirror until the **operator** enables egress
(`guest_egress = true` on the `[[local_libvirt]]` block in the operator's systems inventory —
not a per-request knob), or you use an image that already bakes the toolchain. See the
[systems guide](../guide/toolsets/systems.md#reaching-the-guest-over-ssh).

## Route 3 — the reproducer loop stays root SSH

Provoking a race usually means running the reproducer many times, or under stress, until the
window hits. That loop is your own code, run over root SSH — compile it in-guest or
cross-compile and `scp` the binary in, then run it, `stress-ng`, or a fuzzer over SSH. See the
[reproduce-and-capture loop](../guide/agent-index.md#the-reproduce-and-capture-loop) in the
agent index. A repeat-until-crash-signal primitive is tracked separately (#984); until it
lands, the loop is guest-side SSH.

**A panic drops your SSH channel.** When the kernel crashes, the SSH session dies with it, so
anything you were watching over SSH (a `trace_pipe` tail, a drgn poll) is gone. The
**serial-console sidecar is the durable record** — read it with `runs.get` and the `artifacts`
tools, which persist across the crash. Do not rely on SSH output as your capture of a panic.

## When you _do_ need an out-of-band tool: a dead or hung guest

The tools above all assume a **live, reachable** guest — SSH answers, drgn can attach, the
reproducer runs. When the guest is **dead or hung** and SSH cannot reach it, root SSH is no
help, and that is exactly where an out-of-band MCP tool earns its place. These are the
canonical positive examples of the out-of-band rule (see the `control` toolset guide and the
[four-method live run](runbooks/four-method-live-run.md) runbook):

- `control.force_crash` — forces a panic by injecting an **NMI via libvirt**, not
  `echo c > /proc/sysrq-trigger` over SSH. It crashes a **hung** guest that SSH can no longer
  reach, producing a vmcore you capture with `vmcore.fetch` and triage.
- `control.diagnostic_sysrq` — sends a diagnostic SysRq key with libvirt **`sendKey`** to the
  guest console, not through `/proc/sysrq-trigger`. It provokes a task-state or memory dump on
  a guest whose SSH is wedged.
- `control.power` (`reset`) — power-cycles a wedged-but-`READY` guest through libvirt when it
  stops responding to SSH.

Each of these does something SSH structurally cannot: act on a guest that is not answering.
That is the line. Anything you can do over root SSH stays a documented prompt pattern (the
routes above); anything that must work when SSH is gone is where a tool is warranted.

## See also

- [introspect toolset](../guide/toolsets/introspect.md) — drgn-live and offline introspection.
- [debug toolset](../guide/toolsets/debug.md) — the halting gdbstub path (deterministic bugs).
- [control toolset](../guide/toolsets/control.md) — `force_crash`, `diagnostic_sysrq`, `power`.
- [Agent index](../guide/agent-index.md) — the guest-is-yours contract and reproduce-and-capture loop.
- [Four-method live run](runbooks/four-method-live-run.md) — capture methods end to end.
- [ADR-0366](../adr/0366-race-debugging-out-of-band.md) — resolving #986 as docs, not a code mode.
