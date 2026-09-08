# control toolset

Use these tools for console observation, diagnostics, power actions, and packet capture.
Each operation returns a job handle. Poll with `jobs.wait` and inspect terminal status and
`refs.result`; a successful job does not always mean the guest reached the state you expected.
For parameters and limits, read the tool's current schema.

## Check state and ownership

`control.force_crash`, `control.diagnostic_sysrq`, `control.watch_for_crash`, and
`control.capture_traffic` require a READY System. `control.power` requires READY for
`on`, `off`, `cycle`, or `reset`, and PAUSED for `resume`. Every tool requires contributor
access except `control.force_crash`, which requires the project's admin role and the
profile's destructive-operation opt-in. Provider capabilities also apply.

A READY System can still be restricted by an external boot activation. Power and diagnostic
SysRq are refused while that restriction exists. Force-crash and watch requests must name
the owning `run_id`; traffic capture already takes a Run ID. Other activation states can
refuse these operations too. Follow the returned state/recovery guidance; changing tools
or omitting the Run does not bypass the restriction.

## Crash and diagnostic evidence

- **`control.force_crash`** requests a deliberate panic through the provider and advances
  KDIVE's crash state. It does not capture a vmcore or prove that the guest produced one.
  Wait for its job, check System state and console evidence, then use `vmcore.fetch` when
  the System is CRASHED. Wait for capture success before analysis. See
  resource://kdive/docs/guide/toolsets/postmortem.md.
- **`control.diagnostic_sysrq`** sends an allowed diagnostic command and collects console
  output. Local-libvirt and remote-libvirt support it. On job success, `refs.result` is
  the redacted console artifact ID; read it with `artifacts.get`. Guest configuration can
  reject the command or prevent output, so inspect failures rather than assuming a dump.
- **`control.watch_for_crash`** observes console signatures while your workload runs.
  After its job succeeds, parse the `refs.result` JSON for `fired` or `not_fired`; a match
  can be a non-fatal diagnostic. The watch does not mark the System CRASHED. For observation
  windows, concurrent reproduction, and repeat requests, read
  resource://kdive/docs/operating/race-debugging.md.

## Packet capture

`control.capture_traffic` takes a Run bound to a READY, capture-capable System. Local-libvirt
captures its SSH-forward netdev; remote-libvirt captures the first aliased guest interface.
Neither selects every interface on a multi-interface guest. Run the traffic-producing
workload while capture is active, then wait for the job to finish.

The worker stops after its duration, an observed size threshold, or cancellation. `snaplen`
limits bytes retained per packet; it does not guarantee that packet payloads are excluded.
The optional BPF `capture_filter` is applied after collection, so it does not limit which
traffic is initially captured. Check the schema for bounds and repeat with a new request
when another capture is needed.

On success, `refs.result` is the Run-owned pcap artifact ID. Fetch it with
`artifacts.fetch_raw(run_id, asset="pcap", artifact_id=<refs.result>)`; packet captures are
sensitive and URL-only, so `artifacts.get` will not return them inline. An empty pcap can be
a successful capture with no matching packets. Cancellation does not promise a usable pcap.

## Recovering a guest

For an unrestricted READY System, `control.power(action="reset")` can recover a hung guest;
`resume` returns a PAUSED System to READY after a paused restore. Preserve needed evidence
before either action. Power job success alone does not prove guest boot or SSH readiness.

For a CRASHED System, capture evidence before teardown or reprovisioning. For other states,
inspect `systems.get` and the failed job's recovery guidance. Reinstalling and booting is not
a universal recovery path; state and external-boot gates still apply. A FAILED System cannot
use ordinary teardown today and needs operator confirmation of provider cleanup. See
resource://kdive/docs/guide/toolsets/systems.md for lifecycle operations.
