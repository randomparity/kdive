# Revision-bound live proof preflight (#2752)

## Status

Approved scope: use ADR-0482's existing strict skew policy for revision-bound
proofs. The ordinary default remains unchanged.

## Problem and scope

A live proof can run when an exercised process reports an unknown revision.
The current default warning policy is intentional, but it cannot establish a
proof tied to a deployed revision. The portable stack has three roles; its
absent Kubernetes-only lifecycle witness is inapplicable.

## Design

`require_stack(revision_bound=True)` selects the existing strict policy for
that call, regardless of the ambient policy setting. It skips the proof for
any non-fresh applicable process. Ordinary callers retain the ambient policy.
The skew probe retains each process's reported commit in its result, and the
integration pytest report header lists those values when a stack URL is set.
An absent portable witness is labelled inapplicable, not a missing revision.

The runbook instructs operators to use strict mode for revision-bound live
proof runs, inspect the reported revisions and passed/skipped counts, and never
claim a skipped proof. Remote revision reporting and a global policy change
remain separately owned exclusions.

## Verification

- Focused test: an unknown worker skips in revision-bound mode even when the
  ambient policy is `off`; the ordinary default still warns and runs.
- Focused test: an absent portable witness does not skip strict mode.
- Focused test: a probe records revisions and the pytest hook renders them.
- Documentation: inspect the runbook instruction and examples against the
  implemented API and recipe.
