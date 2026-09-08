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
- `control.diagnostic_sysrq` — send a diagnostic SysRq key to a ready system whose provider
  supports SysRq injection (today local-libvirt) to provoke kernel diagnostics (for example a
  task-state or memory dump) without destroying it.

## Catching a crash you provoke

- `control.watch_for_crash` watches a READY System's console for a recognized kernel
  diagnostic or crash signature. It requires contributor access and provider crash-watch
  support; pass the owning `run_id` when an active external boot restricts the System.
  It returns a job: run the reproducer while the watch is active, poll `jobs.wait`, then
  parse `refs.result` for `outcome` (`fired` or `not_fired`). A successful job does not
  itself mean a signature matched, and a match need not be fatal. The baseline starts at
  worker pickup; `not_fired` plus an SSH disconnect does not prove a crash outside that
  window. Read the console evidence and check System state before capture: the watch does
  not mark it CRASHED. The full concurrent workflow is in
  resource://kdive/docs/operating/race-debugging.md.

## Capturing guest traffic

- `control.capture_traffic` — capture host-side network traffic from a Run's bound **READY**
  traffic-capture-capable guest into a Run-owned pcap, for a bounded window. Only the guest's SSH-forward
  netdev is visible (the guest runs on a restricted user-mode network), so this sees the traffic
  on that path, not arbitrary guest egress. Contributor-level; it enqueues a fixed-duration job —
  poll `jobs.wait`, then fetch the pcap with `artifacts.fetch_raw(run_id, asset="pcap",
  artifact_id=<refs.result>)`. The pcap is sensitive, so it is URL-only (never inline). An optional
  BPF `capture_filter` (e.g. `tcp port 80`) trims the capture; `snaplen` bounds bytes per packet.
  Check `systems.get`'s `supports_traffic_capture` before calling.

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
