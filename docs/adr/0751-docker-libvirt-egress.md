# 0751 — Refuse the known Docker forwarding conflict during remote setup

## Status

Accepted (2026-10-07)

## Context

Issue #3093 reproduced a remote guest timeout on a Docker-enabled Rocky 10
provider. The guest acquired its libvirt network route. Libvirt's native nftables
chain accepted outgoing packets, but Docker's separate IPv4 `FORWARD` chain
counted their drop. The same HTTPS request succeeded on the provider host.
Existing remote setup emits inventory without checking this interaction.

## Decision

Add a read-only, bounded preflight to remote `site.yml`, after firewall
reconciliation and before inventory emission. Recognize the observed iptables
signature: `FORWARD` policy `DROP`, only the unconditional `DOCKER-USER` then
`DOCKER-FORWARD` jumps, and an empty `DOCKER-USER` chain (or its terminal return).
Fail setup with the selected network and an actionable explanation. Do not
change forwarding policy, install rules, or interpret arbitrary rule graphs.

Other rule layouts are outside this signature check, not passing guest-egress
evidence. Operators must verify the actual guest-to-object-store path and a
remote install. The runbook explains that any administrator-owned exception must
be bounded to the chosen guest network and intended destination/return flow,
with persistence owned by that administrator's firewall configuration.

## Consequences

The reproduced default conflict stops before a ready inventory can be emitted.
Existing custom policies remain operator-owned. A successful setup does not
attest their effect, DNS, routes, object-store URLs, or IPv6 forwarding. No
local-host installer contract changes. Actual failed or blocked release cells
remain failed or blocked; this guard does not qualify a remote lifecycle.

## Considered & rejected

- Do nothing — **verified:** issue #3093's native Rocky 10 reproduction had
  guest curl exit 28 while the provider request exited 0; the libvirt accept and
  Docker drop counters matched. Setup currently reaches inventory emission.
- Globally accept forwarding or disable Docker firewall management —
  **judgment:** exceeds the approved scoped-egress boundary.
- Install persistent selected-bridge exceptions — **judgment:** adds firewall
  ownership and restart/reload reconciliation when the issue explicitly permits
  actionable preflight failure.
- Parse arbitrary firewall policy — **judgment:** a general rule interpreter
  is disproportionate and cannot replace the required real guest transfer.
