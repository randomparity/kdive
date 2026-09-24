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
The skew probe retains each process's reported commit in its result, and the
integration pytest report header lists those values when a stack URL is set.
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
- Documentation: inspect the runbook instruction and examples against the
  implemented API and recipe.
