# 0784 — Settled canceled-install build-use release

## Status

Accepted (2026-10-16)

Implements the operator-approved #3241 owner-release design.

## Context

`jobs.cancel` preserves an install job's worker and attempt but changes its state.
The running-only release predicate then retains the build-use pin even after the
provider thread exits. A terminal job state alone does not prove provider settlement.

## Decision

Permit running or canceled jobs in the existing credential-bound release function,
preserving active incarnation, exact holder/current attempt, locks and deletion fences.
Use a new migration0172, preserving SECURITY DEFINER, empty search_path and existing
worker-only EXECUTE without new grants. Install cleanup continues to drain provider
work before release, including cancellation and provider faults. Denial remains false
with the pin retained; stale/reclaimed/dead-worker pins retain operator death recovery.

## Consequences

Cooperatively canceled installs can release after in-process provider settlement.
No general durable finalization, external-descendant or cross-replica termination
proof is added. Existing canceled pins are not bulk-released. Restoring running-only
release requires a new forward migration and reintroduces the known leak.

## Considered & rejected

- **Keep running-only release.** verified: migration0105's release function requires
  `jobs.state = 'running'`; `cancel_job` writes canceled while retaining ownership.
  The issue3241 real-LOGIN-role diagnostic on commit82ce88943 recorded release=false,
  pin retained after actual cancel. This fails the requested cooperative release outcome.
- **Release immediately when cancel is requested.** judgment: terminal job state is
  insufficient settlement evidence for an install provider still consuming artifacts.
- **Redesign cancellation/finalization.** judgment: unnecessary for this bounded exact-owner
  release and outside the approved exclusions owned by #2681 and existing lifecycle owners.
