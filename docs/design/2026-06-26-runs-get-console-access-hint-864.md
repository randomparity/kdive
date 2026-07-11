# Surface a console read-path hint on `runs.get` (#864)

- **Status:** Accepted
- **Date:** 2026-06-26
- **Issue:** [#864](https://github.com/randomparity/kdive/issues/864) — Surface a
  full-log fetch hint on the `runs.get` console ref.
- **ADR:** [ADR-0262](../adr/0262-runs-get-console-access-hint.md)

## Problem

`runs.get` surfaces the boot step's console-evidence artifact as `refs.console`
(ADR-0226), a bare artifact id. The envelope gives an agent no signal about *how* to
read it. The `_run_artifact_refs` docstring mentions only `artifacts.get`, so console
access reads as "fetch one window" — an agent does not learn it can run a targeted
search (`artifacts.search_text`) or page the whole log. The discoverability gap forces
the read strategy to come from out-of-band knowledge.

Black-box review (`BLACK_BOX_REVIEW.md`, §8, 🟢 minor).

## Correction to the issue's premise (verified against `main`)

The issue proposes pointing the console ref at `artifacts.fetch_raw` for "full text."
That tool cannot serve the console artifact:

- `artifacts.fetch_raw` egresses only a **closed `RawAsset` allow-list — `vmcore` and
  `vmlinux`** (`src/kdive/mcp/tools/catalog/artifacts/raw_fetch.py:41-45,81-138`). It
  is keyed by `run_id` + `asset` (an enum), not by an artifact id, and mints a
  presigned download URL for those multi-GB binaries. No code path resolves or serves
  the console artifact id.
- The console ref is the boot step's **REDACTED** console-evidence artifact
  (`src/kdive/services/runs/steps.py:200`, surfaced at `runs/common.py:232`). Its
  real read paths are `artifacts.get` (windowed inline, paged via
  `next_offset`/`content_truncated`, `reads.py`) and `artifacts.search_text`
  (`reads.py:326-392`, takes `artifact_id`).
- Role mismatch: `runs.get`, `artifacts.get`, and `artifacts.search_text` are all
  **VIEWER**; `artifacts.fetch_raw` is **CONTRIBUTOR**
  (`src/kdive/mcp/exposure.py:98-101,191`). A console-ref viewer cannot call
  `fetch_raw`.

So the honest hint names the two tools that actually serve the redacted console
artifact, and `fetch_raw` is deliberately excluded.

## Current behavior (verified against `main`)

- `envelope_for_run` (`src/kdive/mcp/tools/lifecycle/runs/common.py:125-235`) builds
  the `runs.get` success envelope; on the `SUCCEEDED` path it computes
  `console_ref = step_progress.console_evidence_artifact_id` (`:232`) and passes it to
  `_run_artifact_refs`, which sets `refs["console"]` when non-`None` (`:82-99`).
- No `data` field or `suggested_next_actions` entry tells the agent how to read that
  ref. The `_run_artifact_refs` docstring names only `artifacts.get`.

## Requirement

When `runs.get` surfaces `refs.console` (i.e. the boot step recorded console
evidence), the envelope also carries a structured `data.console_access` affordance that
names the two VIEWER-accessible read paths for the redacted console artifact:

- targeted search → `artifacts.search_text`
- full text → `artifacts.get` (paged via `next_offset` until `content_truncated` is
  `"false"`; ADR-0247's per-window cap means whole-log = paging, not one fetch)

The affordance is present **only** when `refs.console` is present, names only literal
valid tool identifiers (not prose), and never names `artifacts.fetch_raw`.

## Approach

`console_ref` is already computed in `envelope_for_run` for the `refs` slot. Add a
module-level `_CONSOLE_ACCESS_HINT` constant and, when `console_ref is not None`, set
`data["console_access"]` to a fresh copy of it:

```python
_CONSOLE_ACCESS_HINT = {
    "ref": "console",
    "search": "artifacts.search_text",
    "full_text": "artifacts.get",
}
```

`ref: "console"` ties the affordance to the `refs["console"]` entry. The values are
literal tool names. A fresh `dict` copy per envelope keeps the shared constant
immutable. The `_run_artifact_refs` docstring is updated to reference both read paths
and the `console_access` affordance, and to record that `fetch_raw` is excluded.

`data` (not `suggested_next_actions`) is the home because the affordance describes how
to read one ref, not a lifecycle step to advance the Run; it matches the existing
descriptive `data` annotations (`available_capture`, `inert_capture_reason`,
`boot_outcome`). `refs` is `dict[str, str]` keyed name→artifact-id and cannot carry
tool-name values without breaking that invariant.

## Acceptance criteria

- [ ] `runs.get` on a `SUCCEEDED` run whose `boot` step recorded an
      `evidence_artifact_id` returns `data.console_access ==
      {"ref": "console", "search": "artifacts.search_text", "full_text": "artifacts.get"}`
      alongside `refs.console` equal to that id, for both `ready` and
      `expected_crash_observed` boot outcomes.
- [ ] The affordance values are the literal tool identifiers `artifacts.search_text`
      and `artifacts.get`; `artifacts.fetch_raw` never appears in it.
- [ ] `runs.get` on a run with no recorded console evidence (no boot step, or a boot
      step whose result has no `evidence_artifact_id`) has **no** `console_access` key
      in `data` (and no `refs.console`).
- [ ] Non-`SUCCEEDED` and failed-run envelopes, and `runs.list`, are unchanged (they
      pass no `step_progress`, so `console_ref` is `None`).

## Edge cases

- **No boot step / not yet booted** → `step_progress` reports
  `console_evidence_artifact_id=None` → no `console_access`.
- **`ready` boot with no console capture** → boot result has no `evidence_artifact_id`
  → `None` → no `console_access`.
- **`FAILED` run** → rendered by `_failed_envelope`, which never sets `console_ref` →
  no `console_access`.
- **Shared-constant mutation** → each envelope copies the constant, so a caller
  mutating its `data` cannot corrupt later responses.

## Out of scope

- Surfacing `artifacts.fetch_raw` for the `debuginfo`/vmcore refs (a separate,
  contributor-gated raw-egress path; different refs, different issue).
- `runs.list` console affordance (it deliberately runs no per-run step query).
- Any change to `artifacts.*` tools, RBAC, schema, or DB (wiring/docs only).
