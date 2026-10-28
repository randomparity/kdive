# 0808 — A definite authority rejection ends the lifecycle wait

## Status

Accepted (2026-10-28)

## Context

The worker dispatches remote module PREPARE and lifecycle requests to the provider authority
through a wait that never abandons an ambiguous dispatch: a transport loss after the send may
have admitted the request, so the wait retries and the authority answers a replay from its
journal. The wait treated every `INFRASTRUCTURE_FAILURE` as that ambiguity, and once it was
ambiguous it swallowed every later categorized error too (#3391).

The authority's `superseded` rejection maps to `INFRASTRUCTURE_FAILURE` in the sender
([ADR-0606](0606-resource-bound-provider-authority-network-route.md)). A rejection that proves the
authority did not admit the request therefore looked like a transport fault, and the wait retried
it every 0.1 s with no deadline and no log. The live remote lane logged 2179 bare rejections in
five minutes, and the job stayed `running` with no failure context. The rejection's reason was
invisible as well, because the JSON log formatter drops `extra` fields
([ADR-0014](0014-structured-logging.md)).

The wire carries only a bare category. The authority's `journal_conflict` reason never reaches the
worker.

## Decision

1. **The sender keeps the error category and marks `details`.** `INFRASTRUCTURE_FAILURE` is
   retryable and the job queue reads that one table (ADR-0483), so a stale-authority attempt still
   retries on a fresh authority. `details["admission"]` is `refused` for `unauthenticated`,
   `superseded`, `provider-not-configured` and `invalid-request`: the authority, or the sender
   itself, answered before it admitted the request. It is `unknown` for `journal-conflict`, whose
   reasons mix pre-mutation and post-mutation checks. Only `provider-conflict` carries
   `authority_reason` ([ADR-0714](0714-repeated-provider-conflict-ends-the-external-boot-job.md)).
2. **One shared wait serves both paths, with three exits.**
   - A refused request on first sight ends the wait with `completion: refused-before-mutation`.
   - A refused request after an earlier ambiguity, or a journal conflict at any time, ends the
     wait with `completion: unknown`. The earlier dispatch may have run, so the failure never
     claims otherwise. No further dispatch is sent, and the job's own retry replays idempotently.
   - An ambiguous dispatch backs off from 0.1 s towards 2 s with up to 25% jitter and ends at the
     operation's existing deadline with `completion: unknown`.
3. **The wait logs.** The first ambiguity, each change of its cause, and every 30 s of an
   unchanged one produce a WARNING with the attempt count. Every exit logs its cause, and so does
   a swallowed unexpected exception. Cancellation stays shielded: a cancelled wait is ambiguous,
   not abandoned.
4. **`failure_context`, the result models and the SQL are unchanged.** The marks live in
   `CategorizedError.details` and the log only.

## Consequences

- A definite rejection fails the job visibly instead of leaving it `running`, and the job's
  ordinary retry policy decides what happens next.
- An ambiguous dispatch is bounded by its deadline instead of looping for ever. Ending it
  releases nothing: the failure reads `unknown`, the lifecycle obligation stays retained and the
  next attempt reads the host's completion record.
- The worker still cannot tell the journal conflicts apart. Surfacing the reason needs a protocol
  change that this decision does not make.
- The rejection log fields depend on the logging amendment of ADR-0014.

## Considered & rejected

- **Map `superseded` to a non-retryable category.** judgment: the category table also decides job
  retry and the MCP `retryable` flag. A stale authority is retried on a fresh one, so the change
  would stop that for every consumer.
- **Treat a refused request after an ambiguity as refused too.** judgment: if the first dispatch
  completed and a takeover then moved the authority on, the retry is rejected while the mutation
  happened.
- **Widen `failure_context.authority_reason`.** judgment: the model admits one literal and
  migration 0170 reads it. The marks are for the wait, and the log carries them.
