# ADR 0226 — Surface the console evidence artifact as `refs.console` on `runs.get`

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-23
- **Deciders:** kdive maintainers
- **Builds on:** [ADR-0179](0179-run-state-step-progress-semantics.md) (the
  `run_steps` step-progress read this extends to carry the boot evidence id),
  [ADR-0141](0141-failed-run-reason-surfacing.md)
  (the `runs.get` envelope shape it extends).

## Context

`runs.get` returns the uniform tool envelope whose `refs` slot already advertises the
Run's `kernel` and `debuginfo` object-store keys (`_run_artifact_refs`,
`src/kdive/mcp/tools/lifecycle/runs/common.py`). It does **not** advertise the console
artifact. An agent chasing crash evidence — most acutely the early-boot
console-crash case (#734) — must therefore make a second `artifacts.list` call against
the System and filter for the console artifact (defect **D5** in the black-box
review).

The console artifact id already exists on the read path. The boot handler writes
`evidence_artifact_id` into the `boot` step's `run_steps.result`
(`jobs/handlers/runs_boot.py`) on both terminal boot outcomes — `ready` (when a
console artifact was captured) and `expected_crash_observed` (the matched evidence,
always present). Console artifacts are System-owned (`owner_kind='systems'`), so the
id is not a `Run` column; the boot step result is the only run-scoped join key.

`runs.get` already loads that boot row: for a `SUCCEEDED` Run it calls
`step_progress(conn, run.id)` (`services/runs/steps.py`), which SELECTs the `boot`
row `result` to extract `boot_outcome`, and passes the result into
`envelope_for_run`. The `evidence_artifact_id` sits in the same JSON the same query
already reads. No new query, table, or column is needed — only the read of a field
already in hand, threaded to the envelope.

The bounding constraint: the write to `runs/common.py` is owned solely by this issue
(#735); #734 is implemented in parallel and only *reads* `expected_boot_failure` from
this file. The change to `_run_artifact_refs` must stay surgical so the two PRs do not
conflict, and #735 merges first.

## Decision

Thread the boot step's `evidence_artifact_id` into the `runs.get` envelope as
`refs.console`, reusing the boot-row read that already happens:

1. **Carry the id on `StepProgress`.** Add
   `console_evidence_artifact_id: str | None` to the `StepProgress` dataclass
   (`services/runs/steps.py`). `step_progress()` populates it from the same `boot`
   row `result` it already reads for `boot_outcome`, via the existing `_optional_str`
   guard — so a missing row, a missing key, or a non-string value all yield `None`.
   No new DB round-trip.

2. **Add an opt-in `console_ref` to `_run_artifact_refs`.** `_run_artifact_refs`
   gains a keyword-only `console_ref: str | None = None`; when non-`None` it adds
   `refs["console"] = console_ref`. The `Run`-derived `kernel`/`debuginfo` keys are
   unchanged. The default keeps every existing caller's behavior identical.

3. **Populate it only on the `SUCCEEDED` success path.** `envelope_for_run` passes
   `step_progress.console_evidence_artifact_id` into `_run_artifact_refs` on the
   `SUCCEEDED` branch, where `step_progress` is in scope. The failed-run envelope
   (`_failed_envelope`) and the `runs.list` path call `_run_artifact_refs(run)` with
   `console_ref` defaulting to `None`, so they are untouched.

`refs.console` is scoped to `runs.get` because it is the only caller that loads the
boot step. The returned id is the console artifact's primary key, so an agent resolves
it directly with `artifacts.get` — one fewer hop than `artifacts.list` + filter.

## Consequences

- `runs.get` on a `SUCCEEDED` Run whose boot step recorded evidence gains
  `refs.console`. A Run not yet booted, or a `ready` boot that captured no console
  artifact, has no `console` key — consistent with how absent `kernel`/`debuginfo`
  refs are simply omitted.
- The id is an unvalidated pointer, exactly like `kernel`/`debuginfo`: `runs.get` does
  not confirm the artifact still exists. A stale id surfaces as a `not_found` on the
  caller's `artifacts.get`, which is the same contract the other refs already carry.
- No MCP request-shape, schema, migration, authz, or dependency change. The envelope
  gains one optional `refs` key. The `boot` row read is the one `step_progress`
  already performs, so there is no added query cost.
- `runs.list` deliberately does not gain `refs.console`: adding it would require a
  per-Run boot-step query on the list path, which this issue does not justify.
- The id is a UUID primary key, not guest/console output, so no redaction applies —
  the console *bytes* were already redacted at capture before persistence; the id is
  metadata.

## Alternatives considered

- **Resolve the console artifact by `owner_kind='systems'` + a fresh
  `artifacts` query in the view.** Rejected: it adds a second DB round-trip and
  re-derives a join the boot step already recorded, and it would need a tiebreak when
  a System has multiple console artifacts across Runs — the boot step result already
  names the *correct* one for this Run.
- **Put `refs.console` on `runs.list` too.** Rejected for this issue: `runs.list`
  intentionally avoids per-Run step queries to stay cheap; adding one for every listed
  Run is a cost the D5 defect (a single-run evidence chase) does not warrant.
- **Validate the artifact exists at view time.** Rejected: inconsistent with
  `kernel`/`debuginfo`, which are also unvalidated pointers, and it would add a
  store/DB lookup to a read that is meant to be a thin pointer hand-off.
- **Surface the id in `data` instead of `refs`.** Rejected: it *is* an object-store
  artifact key, which is exactly what the `refs` slot is for; putting it in `data`
  would split artifact pointers across two envelope slots.
