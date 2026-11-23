# 0842 — Stalled teardown repair advances past skipped Systems

## Status

Proposed

## Context

The stalled-teardown repair always selects the first 100 eligible System IDs, then checks
ownership under each System lock. A persistently authority-marked job or unsettled activation
is skipped without leaving that selector. A real 101-System reconciler proof produced `[0, 0]`
queued jobs over two passes instead of `[0, 1]`; the healthy-only control produced `[1, 0]`.
The later System is valid work, but a full skipped page prevents its inspection.

[ADR-0839](0839-external-boot-repair-keyset-progress.md) already uses per-runner keyset hints
for external-boot repair. [ADR-0841](0841-teardown-warning-history-belongs-to-the-reconciler.md)
provides a private catalog carrier for runtime observation references, separate from config.

## Decision

Give each reconciler a `StalledTeardownProgress` with an optional last-inspected System UUID.
Pass it through the existing carrier and stalled callback. Standalone callers can supply one
progress object across passes; omission remains one-shot. Preserve existing warning/cursor APIs.

Read at most 100 IDs after the hint, retaining the original eligibility and System-ID ordering.
Use a NULL-aware predicate so UUID zero is a real candidate, not a sentinel. An empty tail with
a prior hint permits one wrapped lookup. Process at most one nonempty page per pass; reset the
hint only after a successful all-empty lookup. A lookup failure preserves prior state.

Keep each candidate's existing guarded transaction under the System advisory lock. Extract it
to a private bool-returning helper; false means inspected without admission. Advance only after
an ordinary result or caught candidate exception. Cancellation or an unhandled exception leaves
the interrupted candidate for retry. Count only admissions whose transaction completed.

Keep warning history bounded by the selected page, as before. Page eviction can cause a warning
again after wrap; no persistent full-backlog suppression is added. Change the retry log's
next-pass claim to later-pass, and remove the catalog's premature bounded-replay claim.
The existing terminal recycle policy remains unchanged; its durable replay allowance is separate.

## Consequences

A stable finite page of skipped or failing Systems yields to later work over normal repeated
passes. Hints are not safety fences, durable receipts or a wall-clock deadline. Restart resets
them; new lower IDs wait for wrap, and manual overlapping passes can revisit candidates.
The existing database ownership, cancellation, authority and enqueue guards remain safety owners.

## Considered & rejected

- **Raise the limit or remove skipped owners.** verified: a larger finite prefix can still fill,
  and the skipped rows remain owned by their existing authority or operator recovery path.
- **Checkpoint the selected page before processing.** verified: cancellation can interrupt the
  first candidate; a page checkpoint would postpone uninspected work.
- **Persist another scheduling table.** judgment: this observational hint needs no durable owner
  or schema change. [ADR-0839](0839-external-boot-repair-keyset-progress.md) supplies the pattern.
