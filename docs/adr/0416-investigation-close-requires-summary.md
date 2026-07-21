# ADR-0416: closing an Investigation requires a summary (#1349)

- Status: Accepted
- Date: 2026-07-21

## Context

An Investigation groups Runs toward a goal and ends when an agent drives it to `closed` via
`investigations.close`. Today that tool takes only `investigation_id`: the terminal transition
records nothing about what the investigation found or concluded. The account of the work lives
only in whatever the agent chose to write, if anything, into the free-form `description` — a
field that is editable at any time via `investigations.set` and carries no close-time
obligation. So a closed Investigation can carry no durable statement of its outcome, and a
reader after the fact has no recorded conclusion to read back.

`Investigation` already has a mutable `description: str | None` (`domain/lifecycle/records.py`,
migration 0037). The maintainer decision is that a close-time summary and the general
description are **different intents**: `description` is the anytime-editable working note; a
summary is the terminal account produced *because* the investigation is being closed. Folding
one into the other would either overload `description` with two meanings or lose the
distinction that a summary is captured at, and required by, the close transition.

## Decision

Add a new, distinct nullable `summary` column to `investigations` (migration 0074, additive
and forward-only), separate from `description`. `description` is unchanged.

`investigations.close` gains a **required** `summary` argument. The requirement is enforced at
two layers:

- The registered tool wrapper declares `summary` as a required parameter with no default, so a
  call omitting it is rejected by the tool schema before any handler runs — the agent-visible
  contract states in the wrapper docstring and the `summary` `Field` that a non-empty summary
  is required at close and what it is for (the wrapper text is the only surface the agent
  reads).
- The service (`require_summary`) rejects a blank (empty or whitespace-only) summary with a
  fail-fast `missing_required_field` error, and an over-`SUMMARY_MAX` (4096, matching the
  `description` bound) summary with `invalid_text`, before the row is resolved or locked. A
  blank close therefore never transitions the row.

The summary is persisted on the `open|active -> closed` transition, in the same statement that
stamps `cleanup_pending_at`, and the returned record carries it. The already-closed idempotent
re-close path returns the existing row unchanged, so a second close does **not** overwrite the
originally recorded summary — the summary is a property of the close event, captured once.

`summary` is surfaced in the investigation read envelope (`investigations.get`,
`investigations.close`, and each `investigations.list` item render the same enriched shape), so
an agent reads back exactly what was recorded. The close audit event records the summary's
length (`summary_chars`), not its text, to keep the audit row bounded.

## Consequences

- `investigations.close` is a breaking signature change for any caller that closed with only an
  `investigation_id`; there is no compatibility shim (the project's replace-don't-deprecate
  rule). Every in-tree caller and test is updated in this change.
- Historical closed rows that predate migration 0074 have `summary = NULL`; no summary was ever
  collected for them and none is back-filled. Readers treat NULL as "no summary recorded."
- `description` and `summary` now coexist as two nullable free-text fields with the same length
  bound. The distinction is intent, not shape: `description` is editable at any time and has no
  close obligation; `summary` is write-once at close and required there. The read envelope
  carries both so the difference is legible to an agent.

## Considered & rejected

- **Reuse `description` as the close summary.** Lowest surface — no new column — but it
  overloads one field with two intents (an anytime working note and a terminal conclusion) and
  loses the "required at close" obligation, since `description` is freely editable and
  optional. The maintainer decision is explicit that these are separate intents. Rejected.
- **Keep `summary` optional at close.** A nullable column with an optional argument would let
  the terminal state carry no conclusion — exactly today's gap. The issue's requirement is that
  closing *requires* the agent to produce a summary, so the argument is required and a blank one
  fails fast. Rejected.
- **Enforce the requirement only in the tool schema (required parameter), not the service.**
  The schema rejects an omitted argument, but a whitespace-only string is a syntactically
  present value the schema accepts; without the service check a blank summary would persist.
  Validating in the service too closes that gap and keeps the fail-fast boundary where the
  write happens. Kept both layers.
- **Overwrite the summary on an idempotent re-close.** The close path is idempotent by design
  (a re-close of an already-closed row returns without re-transitioning). Letting a second
  close rewrite the summary would make the recorded conclusion depend on the last redundant
  call. The summary is captured once, on the actual transition. Rejected.
