# 0658 — A missing domain is not a missing SSH forward

## Status

Accepted (2026-09-15)

## Context

[ADR-0298](0298-ssh-reachable-runtime-probe.md) gave the `recorded_ssh_endpoint` port one `None`
return meaning "this System has no recorded SSH forward", rendered by its consumers as
`reason="ssh_not_provisioned"`; the tools themselves come from
[ADR-0271](0271-system-direct-ssh-access.md). The local-libvirt implementation reaches that `None`
from a `try/except` catching *every* `CONFIGURATION_ERROR` the SSH endpoint resolver raises — and
the resolver raises two, one for a connection with no libvirt domain and one for a domain recording
no forward. Both already carry distinct actionable messages; the `except` discards them, so two
conditions with different causes and different fixes arrive as one reason naming neither.

#2480 was misdiagnosed on exactly this: a server reading a different libvirt endpoint than the
worker found no domain, and the surface reported an SSH provisioning gap. Fixing that endpoint
resolution left the misleading message intact. The collapse was never a decision — ADR-0298
specified the no-forward condition only, and the no-domain case reaching the same reason is an
artifact of the breadth of the `except`.

## Decision

We will narrow the swallow rather than widen the reason. Each of the two SSH raise sites carries a
`reason` detail, and `recorded_ssh_endpoint` returns `None` only for the no-forward one. The
no-domain error propagates as the `CONFIGURATION_ERROR` it already is, reaching clients as
`reason="system_domain_not_found"` with a message naming the missing domain and what to check.

`None` keeps its ADR-0298 meaning, narrowed to the single condition that decision described, so
`ssh_not_provisioned` and its detail text are unchanged for every provider. The client contract
change is additive: one new reason where the wrong one was returned before.

## Consequences

- Three MCP tools (`systems.ssh_info`, `systems.authorize_ssh_key`, `systems.check_ssh_reachable`),
  the two SSH worker jobs, and the drgn-live `debug.attach` path surface the new reason with no
  call-site change: each already funnels a `CategorizedError` through `failure_from_error`, which
  merges scalar `details` into the response `data`. On the job path the worker prefixes detail keys,
  so a client reads `failure_detail_reason`. `debug.attach` never swallowed these raise sites, so
  both reason values are genuinely client-facing there.
- A client branching on `data.reason` and treating anything non-`ssh_not_provisioned` as unknown
  now takes its fallback path for a missing domain — the intended correction, and why this is
  recorded rather than patched quietly.
- The same undiscriminated pair exists on this module's gdbstub transport and in remote-libvirt's
  own `recorded_ssh_endpoint`; both are out of scope here and this record does not govern them.

## Considered & rejected

- **Add a discriminating `cause` field beside `ssh_not_provisioned`.** judgment: leaves one reason
  meaning two conditions and obliges every consumer to read a second field to learn which — the
  ambiguity this issue was filed about, relocated rather than removed.
- **Return a result object, or an endpoint-plus-cause tuple, from the port.** verified: the port has
  three implementers and five call sites (`rg -n "def recorded_ssh_endpoint" src/` and
  `rg -n "\.recorded_ssh_endpoint\(" src/` at cd90bc63e), all of which would change. Remote-libvirt
  collapses its own conditions to `None` too (`remote_libvirt/lifecycle/connect.py:152-176`: absent
  parity config, absent domain, and absent recorded port), so the wider signature would be a second
  provider's fix carried by this one — the decomposition #2502 did not ask for.
- **Drop the `None` sentinel entirely and always raise.** judgment: `None` is a truthful answer for
  a provider that exposes no forward at all (fault-inject, remote-libvirt without parity), and
  turning a normal capability answer into an exception would change all three implementers and
  every call site to fix a reporting defect in one of them.
- **Improve only the message text at the raise sites.** verified: both raise sites already carry
  distinct actionable messages at cd90bc63e, and the `except` discards them, so no edit reaches a
  caller.
- **Do nothing.** judgment: the reason already outlived its cause once — what #2502 records.
