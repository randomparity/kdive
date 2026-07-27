# postmortem toolset

These turn a crashed kernel into an analyzable core and then read it. The path is: a Run
crashes, you **capture the vmcore**, then **triage or analyze** it. Reach for these after a
crash (deliberate — see the control guide — or spontaneous). For exact parameters, types, and
return schema, read each tool's own description.

## Capturing the core (`vmcore`)

- `vmcore.fetch` — capture the vmcore from a crashed Run. Pick a capture method or take the
  default; this is the durable crash artifact everything downstream reads. The completed
  capture job carries the redacted core's artifact id in `refs.result`; `runs.get` carries the
  same id as `refs.vmcore` when you no longer hold the job id.

## Analyzing the core

- `postmortem.crash` — read a captured core with crash(8). Omit `commands` for the standard
  first-pass batch: a fast verdict (the panic reason and the faulting context) without you
  writing any crash commands. Pass your own allowlisted read-only commands when you need to go
  past that summary.

For programmable, scripted analysis of the same core, `introspect.from_vmcore` (see the
introspect guide) runs drgn against it instead of crash(8).
