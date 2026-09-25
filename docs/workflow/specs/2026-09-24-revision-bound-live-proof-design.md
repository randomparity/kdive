# Revision-bound live proof preflight (#2752)

## Status

Approved scope: use ADR-0482's existing strict skew policy for revision-bound
proofs. The ordinary default remains unchanged.

## Problem and scope

A live proof can run when an exercised process reports an unknown revision.
The current default warning policy is intentional, but it cannot establish a
proof tied to a deployed revision. The portable stack has three roles; its
absent Kubernetes-only lifecycle witness is inapplicable.

## Failure model

- Actors and deployments: local proof operator and CI job running the portable
  three-role stack with a checkout and its live-stack pytest tests.
- Invariants and assets: a revision-bound passed test must have an identified,
  fresh revision for each applicable process the preflight probes; the report
  must show what the probe observed.
- Accepted failures: a missing stack or non-fresh process skips the proof;
  the run's overall exit may be green with skipped tests, so the operator must
  check the passed/skipped counts before recording proof.
- Covered elsewhere: remote revision exposure is owned by a separate
  architecture decision; the global default warning policy belongs to ADR-0482.

## Design

The revision-bound proof command sets `KDIVE_STACK_SKEW_POLICY=strict` for
its entire run. The existing preflight skips that proof for any non-fresh
applicable process. Ordinary callers retain the default policy.
The skew probe retains each process's reported commit in its result. A pytest
session hook in the root test conftest captures the initial probe before
collection, including runs that select no test path. Verbose runs
use the report-header hook, and quiet runs print the same line at session
start. `KDIVE_STACK_SKEW_POLICY=off` performs no reporting probe.
For a strict proof, admission requires both the header probe and a new probe
at each test gate to be fresh. A stack that becomes fresh only after the header
cannot pass under a header naming its earlier revision; rerun pytest to make
the header and admission agree. Ordinary policy calls retain the existing
session cache and worker-set invalidation.
An absent portable witness is labelled inapplicable, not a missing revision.

The runbook instructs operators to use strict mode for revision-bound live
proof runs, inspect the reported revisions and passed/skipped counts, and never
claim a skipped proof. Remote revision reporting and a global policy change
remain separately owned exclusions.

## Verification

- Focused test: an unknown worker skips in strict mode; the ordinary default
  still warns and runs.
- Focused test: an absent portable witness does not skip strict mode.
- Focused test: a probe records revisions and the pytest hook renders them.
- Focused test: a non-fresh header probe prevents a strict proof from passing
  even if a later admission probe would be fresh.
- Focused test: a server revision change with unchanged worker PIDs is
  detected by a later strict admission probe.
- Focused test: quiet pytest captures and prints the initial revisions;
  `off` never probes for reporting.
- Documentation: inspect the runbook instruction and examples against the
  implemented API and recipe.
