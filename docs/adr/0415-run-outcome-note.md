# ADR-0415: a post-hoc outcome note on a Run (#1386)

- Status: Accepted
- Date: 2026-07-21

## Context

An agent running several Runs under one Investigation (a buggy-kernel repro, a wrong-fix
attempt, a confirmed-fix) cannot record a human-readable verdict per Run after the fact —
"UBSAN reproduced, not a panic", "wrong fix applied", "fix confirmed". The only free-form field
on a `Run` is `label` (`domain/lifecycle/records.py`, migration 0050), which is write-once at
`runs.create` and validated by `validate_label`: it is a create-time handle, set before the
outcome is known. There is no post-hoc-mutable per-Run field and no tool to set one, so
distinguishing Runs after the fact means encoding the verdict into the `label` at creation, when
the outcome does not yet exist.

`Investigation.description` is free-form and post-hoc-mutable via `investigations.set`, so an
agent can record a note at the **grouping** level today — but an Investigation groups many Runs,
so that note cannot capture a per-Run verdict. The maintainer decision is that per-Run
granularity is required and is implemented as a **bespoke** per-Run field, not a generic
annotation seam.

## Decision

Add a new nullable `outcome_note` column to `runs` (migration 0075, additive and forward-only,
ADR-0015), distinct from `label`. `label` is unchanged: it stays the write-once client handle
set at create. `outcome_note` is the post-hoc outcome text.

Add a mutation tool `runs.set(run_id, outcome_note)` — the name mirrors the `investigations.set`
metadata-mutation precedent. It sets or updates `outcome_note` on an existing Run at **any** time
after creation, including on a terminal (`succeeded`/`failed`/`canceled`) Run, because the
verdict is recorded once the outcome is known. There is no state gate; the only authorization is
`contributor` on the Run's project (matching `runs.cancel`). A blank `outcome_note` clears the
note to NULL. The value is length-validated (`OUTCOME_NOTE_MAX_LEN` 4096, matching the
`investigations.summary`/`description` bound and the DB CHECK) and rejected for a NUL; it is the
caller's own free-form input, echoed verbatim like `label`, so it is not run through the secret
redactor. The write happens under the per-Run advisory lock and records an audit event whose
args note only whether the note was `set` or `cleared`, never its text.

`outcome_note` is surfaced in the Run read envelope via the shared `_run_recovery` helper, so it
is echoed as `data.outcome_note` on every Run read path — `runs.get`, `runs.list`, and the
failed-Run envelope — alongside `label`. `None` renders until an agent records one. The agent-
facing contract (the `runs.set` wrapper docstring and the `outcome_note` `Field` description —
the only text the agent sees) states that it is an optional post-hoc note editable at any time,
distinct from the write-once `label`, and that a blank value clears it.

## Consequences

- The `runs.*` surface gains one tool (`runs.set`); the generated tool count and reference docs
  are regenerated.
- Historical Runs that predate migration 0075 have `outcome_note = NULL`; none is back-filled.
  Readers treat NULL as "no note recorded."
- `label` and `outcome_note` coexist as two nullable free-text fields with different lifecycles:
  `label` is write-once at create (a handle), `outcome_note` is anytime-editable post-hoc (a
  verdict). The read envelope carries both so the difference is legible to an agent.

## Considered & rejected

- **A generic annotation seam (arbitrary key/value notes on any resource).** A general
  annotation store would satisfy this request and future ones, but it is speculative surface for
  a single concrete need (the "no speculative features" rule), pushes schema and query
  complexity onto every reader, and gives the agent a less legible contract than a named field.
  The maintainer decision is an explicit bespoke per-Run field. Rejected.
- **Reuse `label` (drop the write-once rule, make it editable).** Lowest surface — no new column
  — but it conflates a create-time handle with a post-hoc verdict and would silently change the
  meaning of every existing `label`. The two intents are kept as separate fields. Rejected.
- **Rely on `Investigation.description` at the grouping level.** Already available, but an
  Investigation groups many Runs, so it cannot record a per-Run verdict — exactly the gap the
  issue names. Rejected.
- **Different lifecycle from #1349's close summary (ADR-0416).** #1349 added a *required*,
  write-once-at-close `summary` on `Investigation`, captured because the investigation is being
  driven terminal. `outcome_note` is deliberately the opposite: *optional*, per-Run, and
  editable at any time with no lifecycle obligation. They are kept conceptually separate — a
  Run's outcome note is not a terminal contract, so it is not gated on any transition and can be
  overwritten or cleared freely.
