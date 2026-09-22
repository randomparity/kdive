# 0672 — Authorize SSH has a longer preflight window

## Status

Accepted (2026-09-22)

## Context

ADR-0305 reused the viewer-facing SSH reachability probe as the
`systems.authorize_ssh_key` preflight and fixed both flows to the same 15-second deadline. That
choice replaced a potentially repeated 90-second SSH retry with one terminal, diagnosable
preflight. Its evidence was a 46-millisecond sshd bind race on the then-supported images.

Issue #825 adds openSUSE Tumbleweed and Leap 15.6 images. Their baseline provisioning exposed two
assumptions behind that evidence. First, QEMU's host forward accepts a TCP connection before guest
sshd exists, so holding one accepted connection for the whole deadline can miss a daemon that
later starts. Second, Leap can start sshd just after the 15-second preflight expires. This is not a
provision-readiness regression: ADR-0272 deliberately makes baseline provision `ready` mean that
the domain started, without waiting for the guest's serial readiness marker.

The reconnect correction alone made Tumbleweed authorization pass live, but Leap still failed
terminally immediately before its console reported sshd started. A supported catalog image must
complete the documented provision → authorize flow without a caller-invented sleep.

## Decision

Keep the viewer-facing `systems.check_ssh_reachable` flow at its existing 15-second deadline. Its
fresh-job queue-pressure and quick-observation contracts from ADR-0298 do not need to expand.

For both flows, cap each SSH-banner read at two seconds. If QEMU accepted the connection but
returned no bytes, close it and reconnect within the flow's overall deadline. A nonempty non-SSH
banner still fails immediately. When the deadline expires, report `tcp_connect` failure only if no
connection ever succeeded; otherwise report `ssh_banner` failure.

Give only the `systems.authorize_ssh_key` preflight a 30-second deadline. The unit is seconds, the
reference clock is the worker's monotonic clock, and the scope is one authorization job. Expiry
causes a terminal `transport_failure`; it does not enter the later 90-second append retry. The
recovery is to confirm the guest now answers with `systems.check_ssh_reachable`, then authorize a
different key or reprovision before retrying because an identical key replays its prior job. State
this complete limit contract in the MCP wrapper docstring.

The deadline remains fixed rather than configurable. It is a compatibility allowance for the
supported guest set, not an operator performance knob.

## Consequences

Supported slow-starting guests have twice the former authorization-preflight window without
doubling the viewer probe's queue occupancy. A doomed authorization can now remain running for up
to about 30 seconds before its terminal failure, plus ordinary job queue and polling latency. Once
a banner is observed, the existing key-load and append behavior is unchanged.

The reconnect loop creates several short-lived loopback connections while sshd starts. It sends no
bytes, retains the existing 255-byte banner cap, and keeps one connection at a time. The fixed
two-second read slice leaves multiple connection attempts inside either flow deadline.

Future catalog images that cannot answer within 30 seconds remain unsupported by the immediate
provision → authorize contract until evidence justifies another decision. They do not silently
fall into the append retry or worker requeue loop ADR-0305 removed.

## Considered & rejected

- **Thirty seconds only for authorization (selected).** verified: on a KVM development system,
  the issue #825 Tumbleweed parameter passed after banner-less reconnects were enabled, while the
  Leap parameter's 15-second terminal verdict preceded its console's successful sshd start. A
  30-second authorization window covers both without changing the observation job.
- **Raise the shared reachability deadline to 30 seconds.** judgment: this would double the bound
  for every VIEWER-enqueued fresh probe and weaken ADR-0298's queue-pressure mitigation when only
  the mutating authorization flow requires more tolerance.
- **Reuse authorization's 90-second append retry.** verified: ADR-0305 records that this path could
  multiply across worker attempts to about 230 seconds. Sending the key append only after a banner
  remains the bounded, diagnosable separation.
- **Add a SUSE-only sleep or readiness-unit drop-in.** verified: ADR-0272 and the local provision
  handler show baseline `ready` is committed after domain start and does not consume the serial
  marker. A family drop-in cannot change that contract, while a test sleep would not fix agents'
  product path.
