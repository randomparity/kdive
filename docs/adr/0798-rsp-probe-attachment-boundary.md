# 0798 — Gdbstub attachment belongs to the debugger

## Status

Accepted (2026-10-16)

Implements the operator-approved #3245 removal design.
Supersedes only ADR-0032 section 6's standalone RSP reachability/noninterference claim.
Other ADR-0032 decisions remain in effect; the accepted record is unchanged.

## Context

The standalone Connect probe creates a gdbstub TCP connection, queries `?`, then closes.
The admitted QEMU 10.2.2 no-disk diagnostic at `82ce88943` observed TCP connection
pause a running guest before the query; disconnect did not resume it. Startup `-S`
stayed halted. That diagnosis is not Linux/provider qualification.

## Decision

Remove the standalone active probe from local and remote gdbstub Connect. Resolve
and validate endpoint metadata and reserve the existing session without debugger IO.
The existing lazy actual GDB attachment owns protocol validation and may halt according
to its present contract. A live session reservation does not guarantee reachability;
subsequent actual attachment reports existing categorized connection failures.

## Consequences

Reservation no longer perturbs guest execution through a throwaway debugger connection.
Unreachable or non-RSP targets are discovered later by the actual attachment. No state
sampling/restoration, replacement TCP probe, detach or unconditional continue is added.
SSH/drgn-live behavior, between-call semantics (#3244) and interrupt behavior (#3230)
retain their present owners. There is no persisted-data migration.

## Considered & rejected

- **Change the read-only query.** verified: the QEMU 10.2.2 two-arm diagnostic at
  `82ce88943` observed the running arm pause immediately on TCP connect before sending `?`.
- **Restore sampled state after probing.** judgment: requires new exclusivity and race
  semantics, while deferring validation to existing attachment removes the extra owner.
- **Unconditionally continue or detach.** judgment: cannot promise preservation of an
  initially halted guest without an authoritative ownership/state contract.
