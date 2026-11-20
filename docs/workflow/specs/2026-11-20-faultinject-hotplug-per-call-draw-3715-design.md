# Fault-inject hot-plug draws once per call (#3715)

Decision record: [ADR-0074](../../adr/0074-fault-inject-engine-port-wiring.md), amended by this
change. Builds on ADR-0072 (`attempt` is durable, never a process-local counter) and #3716.

## Problem

`FaultedHotplug` passes `attempt` fixed at 1. The engine's draw is a pure function of seed,
System, plane and attempt, so a `hotplug` `fault_rate` strictly between 0 and 1 is decided once
per System: every plug and un-plug faults, or none does. A transient hot-plug fault cannot be
modelled, and a faulting System can never un-plug before teardown.

## Scope

In: a per-System call ordinal supplying `attempt` for hot-plug draws, the ADR-0074 amendment,
tests. Out: per-call draws for the provision, install and boot planes (unowned); any schema,
port or resolver contract change.

Ownership: `FaultInjectInventory` already owns the synthetic domain per System and is the one
object every per-binding hot-plug port shares, so it owns the ordinal. `FaultedHotplug` only
asks for the next one.

## Contract

- `FaultInjectInventory.next_draw(domain_name) -> tuple[UUID, int] | None` returns the domain's
  System and its next 1-based ordinal, or `None` for an unknown domain (no draw, as before).
  Lookup and increment are atomic under an inventory lock.
- `record` and `forget` clear the domain's ordinal, so provision, reprovision and teardown
  restart the sequence with the domain.
- `FaultedHotplug` draws `attempt=ordinal` for each plug and un-plug. Its `attempt_for` parameter
  is removed (no caller passed it); `is_attached` and `release_all` still draw nothing.

## Where the ordinal lives, and why

In the fault-inject inventory, as the issue prefers; not in the device row (no migration, no
shared contract). It is in-process state, like the domain registry and the hot-plug ledger it
sits beside: it resets on a worker restart exactly as they do. ADR-0072 rules out a counter
that makes the draw order-dependent across runs; here the sequence for one System is a pure
function of seed, System and the number of prior calls, and replays identically from a fresh
inventory. That is a deliberate narrowing of ADR-0072's "durable" leg for this one synthetic
plane and is stated in the ADR amendment.

## Failure model

- Concurrent plug and un-plug on one System: each takes a distinct ordinal under the lock; which
  call gets which ordinal follows lock order, as for any serialised call.
- Per-binding copies: the inventory is shared by reference, so every binding of one System
  continues the same sequence.
- Worker restart: the in-process ordinal restarts at 1, as the domain registry does.
- A faulted call still consumes its ordinal, so a fault is followed by a fresh draw.

## Tests

Fixed seed, rate 0.5: twelve successive plugs on one System contain both outcomes and replay
identically from a fresh inventory; re-recording the domain restarts the sequence; ports
rebound per operation continue one sequence; inventory unit tests for ordinal, reset and
unknown domain. Controlled fault: pinning the ordinal to 1 turns the mix tests red.
