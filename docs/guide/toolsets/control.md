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

- `control.power` — power actions (`on`/`off`/`cycle`/`reset`) on a **READY** system.
  Contributor leaseholder control over your transient VM, not destructive administration:
  it requires only `contributor` and no `destructive_ops` opt-in. Refused on a non-READY
  system — a `CRASHED` system holds crash evidence and must not be reset through the power
  path.

## Recovering a wedged guest

If a guest stops responding (for example SSH can no longer connect) but the System is still
`READY`, `control.power reset` (contributor) reboots it in place — the first-class recovery.
If the guest will not respond to a reset, or the System is not `READY` (wedged before boot,
or `CRASHED`), fall back to `runs.install` with a changed cmdline + `runs.boot` to re-stage.
For a `CRASHED` System, use the crash workflow instead — `capture_vmcore` (via `vmcore.fetch`)
then `systems.teardown` or `systems.reprovision`.
