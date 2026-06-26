# ADR-0260: surface the matched console line on an expected_boot_failure (#840)

- Status: Accepted
- Date: 2026-06-26

## Context

When a `console_crash` `expected_boot_failure` matches, `runs.get` echoes the Run's
*configured* expectation — `data["expected_boot_failure"]` (the kind) and
`data["expected_boot_failure_detail"]` (the full `{kind, pattern}` dict) — but never the
actual console line that matched. An agent confirming a reproduce therefore cannot tell
whether the boot matched the *intended* crash (e.g. `RIP: 0010:__d_lookup`) or an unrelated
earlier warning that happened to contain the pattern substring. The evidence is weaker than it
needs to be, and the matched line already exists at the point of decision.

`_expected_crash_matches` (`jobs/handlers/runs_boot.py`) calls `search_text(...)` over the
Run's redacted boot-window console and returns only `match_count > 0`, discarding
`SearchResult.matches` — which carries the matched line text (`matches[0]["text"]`,
`security/artifacts/artifact_search.py`). `_record_expected_crash` records the
`expected_crash_observed` boot result (`boot_outcome`, `expectation_matched`,
`evidence_artifact_id`, `available_capture`, `inert_capture`) but not the matched line.
`step_progress` reads that boot result into `StepProgress`, and `envelope_for_run` renders it.

## Decision

Keep the first matched line and thread it through the existing boot-result → `StepProgress` →
`envelope_for_run` read path, mirroring how `available_capture`/`inert_capture` already flow
(ADR-0239):

- `_expected_crash_matches` becomes `_expected_crash_matched_line(run, redacted_console)
  -> str | None`, returning `SearchResult.matches[0]["text"]` on a match and `None` otherwise
  (still failing closed to `None` on a malformed pattern or a non-`console_crash` expectation).
  The caller gates on `is not None`.
- `_record_expected_crash` takes the matched line and records it as `matched_line` on the
  `expected_crash_observed` boot result.
- `StepProgress` gains a `matched_line: str | None` field, populated by `step_progress` from the
  persisted boot result.
- `envelope_for_run` surfaces it as `data["expected_boot_failure_matched_line"]`, inside the
  existing `run.expected_boot_failure is not None` block so it reads alongside
  `expected_boot_failure_detail`; omitted entirely when no line was recorded.

Redaction is preserved by *sourcing*, not by a second pass. The bytes searched are the Run's
already-redacted boot-window console (`_read_redacted_console` runs the `Redactor` before any
match), so `matches[0]["text"]` is redacted at origin — and `search_text` additionally clips it
to `MAX_LINE_CHARS`. The matched line is therefore redacted-and-bounded before it is persisted to
the boot result and before `runs.get` returns it. No new redaction pass runs, matching how the
`evidence_artifact_id` console artifact and the failure-side disclosures are already trusted to
be redacted at capture time.

No new data model, schema, migration, RBAC, or tool-surface change: the boot result is an
existing JSON payload, the new field is additive, and the read path already loads it.

## Consequences

- An agent reading `runs.get` on a matched `expected_boot_failure` sees the exact console line
  that matched, so it can confirm the boot reproduced the *intended* crash rather than an
  unrelated pattern hit.
- The matched line is redacted (searched over already-redacted console bytes) and length-bounded
  (`search_text`'s `_clip`), so surfacing it leaks no secret and cannot blow the response budget.
- The field is present only when an expected crash actually matched; a Run with no
  `expected_boot_failure`, or one that matched nothing, omits it.
- `_expected_crash_matches` changes from a bool to a `str | None` return; the one caller and its
  unit tests move to the `is not None` gate. No persisted value or outcome changes.

## Considered & rejected

- **Add a second function for the line and keep `_expected_crash_matches` as a bool.** Runs the
  same `search_text` twice over the same console for one decision; the single function returning
  `str | None` carries both the match verdict (`is not None`) and the line.
- **Re-run the `Redactor` on the matched line before persisting.** Redundant — the searched bytes
  are already redacted, so a second pass would only re-scan a redacted string. Sourcing from the
  redacted console is the invariant, the same one `evidence_artifact_id` relies on.
- **Surface the full `SearchResult.matches` window (before/after context) on `runs.get`.** The
  console artifact is already fetchable via `refs.console` + `search_text` for full context; the
  read envelope needs only the one identifying line, and a multi-line window would bloat it.
- **Persist a typed model / DB column for the matched line.** The value is a single string already
  produced by the search and stored in the existing boot-result JSON; a column would be redundant
  state and a needless migration.
- **Echo the raw (un-redacted) matched line for fidelity.** Violates the redaction contract —
  console output is untrusted; the redacted line is the only one safe to surface.
