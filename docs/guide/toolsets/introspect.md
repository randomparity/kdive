# introspect toolset

These read kernel state with **drgn** — the programmable, non-halting introspection path.
Unlike the `debug` toolset (which halts the CPU at a GDB stub), introspection reads a running
or captured kernel without stopping it, so it is the race-friendly choice for inspecting live
state or a vmcore. For exact parameters, types, and return schema, read each tool's own
description.

There are two modes: **live** (over SSH against a running guest) and **offline** (against a
captured vmcore).

**Live prerequisites.** Live introspection reaches the guest over the drgn-over-SSH transport,
which needs **no** credential provisioning: the SSH forward is rendered on every domain and the
transport authenticates with the per-System bootstrap key, so any ready local system qualifies.
The only requirement is a drgn-capable guest image and a guest reachable over SSH — if drgn is
absent, `introspect.run` reports `missing_dependency`. `introspect.run` and `introspect.script`
take a live drgn-live `DebugSession`.

## Live introspection

- `introspect.run` — run an in-tree drgn helper (`tasks`, `modules`, `sysinfo`) against a
  live drgn-live session. Start here for common questions.
- `introspect.script` — run your own drgn script against a live session when a helper does not
  cover what you need. This is the supported way to read a **struct field or array member by
  name** (e.g. `some_struct->field[3].member`) on a live guest: drgn resolves typed kernel
  objects by name — `prog["some_struct"].field[3].member` — which the halting `debug` gdbstub
  path (`debug.resolve_symbol` resolves an address only) cannot.

## Offline introspection

- `introspect.from_vmcore` — run drgn introspection against a Run's captured vmcore, with no
  live guest required. Capture the core first with `vmcore.fetch` (see the postmortem guide).
