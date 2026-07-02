# control toolset

These drive the target's power and crash state — most importantly, **how you deliberately
induce a crash** to produce a vmcore to triage. Reach for them when the investigation needs a
crash on demand, a diagnostic dump, or a power cycle. These operations change or destroy guest
state, so they are gated accordingly. For exact parameters, types, and return schema, read
each tool's own description.

## Inducing a crash

- `control.force_crash` — force the guest to panic via NMI, producing a vmcore you then
  capture with `vmcore.fetch` and triage (see the postmortem guide). This is the deliberate
  path to a crash dump.
- `control.diagnostic_sysrq` — send a diagnostic SysRq key to a ready local-libvirt system to
  provoke kernel diagnostics (for example a task-state or memory dump) without destroying it.

## Power

- `control.power` — power actions (such as stop or reset) on a started system.
