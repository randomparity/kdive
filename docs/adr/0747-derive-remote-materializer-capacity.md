# 0747 — Derive remote materializer capacity from admitted artifact bounds

## Status

Accepted (2026-10-08)

## Context

Issue #3133 reproduces production remote materialization refusing every plan:
the 10 GiB ceiling is below the fixed 18,818,269,184-byte archive reservation.
Tests previously supplied a separate 64 GiB ceiling. Existing closed artifact
bounds permit a maximum reservation of 34,055,454,720 bytes before remote metadata.

## Decision

Derive the production remote ceiling from those existing maximum component and
archive bounds plus the materializer's existing 2,097,152-byte metadata allowance:
34,057,551,872 bytes with current bounds. The shared bounds module owns the maximum
artifact reservation; the remote materializer owns its metadata-inclusive ceiling,
and production host assembly passes that ceiling. No operator setting is added.
Keep per-plan accounting, artifact validity limits, ownership and deadline checks,
and the existing overcapacity error unchanged. This is a per-activation admission
bound, not free-space reservation or an aggregate concurrency budget.

## Consequences

All plans within the existing bounds fit production admission. Storage exhaustion
can still fail subsequent I/O; existing provider failure/recovery handling remains.
A production-assembly regression must reach staging with a maximum validated plan,
while a materializer configured one byte below a plan's reservation must refuse
before staging. Remote DWARF placement (#3131) and POWER qualification are separate.

## Considered & rejected

- **Count only typical or declared module archive sizes.** judgment: weakening the
  conservative reservation changes the safety contract unnecessarily.
- **Another literal ceiling or new configuration.** judgment: deriving the exact
  ceiling from existing bounds avoids a second sizing policy and operator input.
