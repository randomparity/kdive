# postmortem toolset

These turn a crashed kernel into an analyzable core and then read it. The path is: a Run
crashes, you **capture the vmcore**, then **triage or analyze** it. Reach for these after a
crash (deliberate — see the control guide — or spontaneous). For exact parameters, types, and
return schema, read each tool's own description.

## Capturing the core (`vmcore`)

- `vmcore.fetch` — capture the vmcore from a crashed Run. Pick a capture method or take the
  default; this is the durable crash artifact everything downstream reads.
- `vmcore.list` — list the vmcores already captured for a Run.

## Analyzing the core

- `postmortem.triage` — auto-triage a captured core: a fast first-pass verdict (the panic
  reason and the faulting context) without you writing any crash commands.
- `postmortem.crash` — run allowlisted read-only crash(8) commands against the captured core
  when you need to go past the triage summary.

For programmable, scripted analysis of the same core, `introspect.from_vmcore` (see the
introspect guide) runs drgn against it instead of crash(8).
