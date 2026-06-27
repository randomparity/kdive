# ADR-0262: surface a console read-path hint on `runs.get` (#864)

- Status: Accepted
- Date: 2026-06-26

## Context

`runs.get` surfaces the boot step's console-evidence artifact as `refs.console`
(ADR-0226) — a bare artifact id with no signal about how to read it. The
`_run_artifact_refs` docstring names only `artifacts.get`, so console access reads as
"fetch one window." An agent does not learn from the envelope that it can run a
targeted search (`artifacts.search_text`) or page the whole log; the read strategy has
to come from out-of-band knowledge (#864, black-box review §8).

Issue #864 proposes pointing the console ref at `artifacts.fetch_raw` for the "full
log." Verified against `main`, `fetch_raw` cannot serve the console artifact:

- It egresses only a **closed `RawAsset` allow-list — `vmcore` and `vmlinux`**
  (`src/kdive/mcp/tools/catalog/artifacts/raw_fetch.py:41-45,81-138`), keyed by
  `run_id` + `asset` (an enum), not by an artifact id, and returns a presigned URL for
  those multi-GB binaries. No code path resolves the console artifact id.
- The console ref is the boot step's **REDACTED** console-evidence artifact
  (`services/runs/steps.py:200`). Its real read paths are `artifacts.get` (windowed,
  paged via `next_offset`/`content_truncated`) and `artifacts.search_text`
  (takes `artifact_id`).
- `runs.get`/`artifacts.get`/`artifacts.search_text` are **VIEWER**;
  `artifacts.fetch_raw` is **CONTRIBUTOR** (`mcp/exposure.py:98-101,191`), so a
  console-ref viewer cannot even call `fetch_raw`.

Surfacing `fetch_raw` on the console ref would document a phantom, permission-mismatched
capability. This ADR records the corrected decision.

## Decision

When `runs.get` surfaces `refs.console`, also surface a structured
`data.console_access` affordance naming the two VIEWER-accessible read paths for the
redacted console artifact, using literal valid tool identifiers:

- `search` → `artifacts.search_text` (targeted query)
- `full_text` → `artifacts.get` (full log by paging `next_offset` until
  `content_truncated` is `"false"`; ADR-0247's per-window cap means whole-log = paging)
- `ref` → `"console"`, tying the affordance to the `refs["console"]` entry

A module-level `_CONSOLE_ACCESS_HINT` constant holds the shape; `envelope_for_run`
copies it into `data["console_access"]` only when `console_ref is not None` (the same
gate that sets `refs["console"]`). The `_run_artifact_refs` docstring is updated to name
both read paths and the affordance, and to record that `fetch_raw` is deliberately
excluded.

`data` is the home (not `suggested_next_actions`) because the affordance describes how
to read one ref, not a lifecycle step that advances the Run; it sits with the existing
descriptive `data` annotations (`available_capture`, `inert_capture_reason`,
`boot_outcome`). `fetch_raw` is excluded for the asset-domain and role reasons in
Context.

No schema, migration, RBAC, config, or `artifacts.*` tool-surface change: the affordance
is additive `data` on an existing read path, carrying build-time constant strings only,
so it is redaction-safe.

## Consequences

- An agent reading `runs.get` learns both VIEWER-accessible console read paths from the
  envelope, with no out-of-band knowledge: search via `artifacts.search_text`, full log
  via paged `artifacts.get`.
- The affordance names only tools the console-ref viewer can actually call, so it never
  steers a viewer at a forbidden or inapplicable tool.
- It is present only when `refs.console` is present; runs with no console evidence,
  failed runs, and `runs.list` are unchanged.
- The hint diverges from #864's literal `fetch_raw` wording; the divergence is recorded
  here and in the spec so the next reader does not "fix" it back to the phantom tool.

## Considered & rejected

- **Point the console ref at `artifacts.fetch_raw` (the issue's literal wording).**
  `fetch_raw` egresses only the `vmcore`/`vmlinux` `RawAsset` allow-list, is keyed by
  `run_id`+`asset` (not an artifact id), and is CONTRIBUTOR-gated while the console ref
  is VIEWER-visible. It can neither serve the console artifact nor be called by a
  console-ref viewer — a phantom, permission-mismatched hint.
- **Add the tool names to `suggested_next_actions`.** That list drives lifecycle
  progression (`runs.bind`/`runs.install`/`debug.start_session`); mixing artifact-read
  affordances dilutes it and loses the tie to the specific ref. A structured `data`
  entry keeps progression clean and binds the tools to `refs.console`.
- **Annotate `refs` itself.** `refs` is `dict[str, str]` keyed name→artifact-id; a
  tool-name value would break that invariant and the response model.
- **Embed paging prose in the affordance value.** Affordance fields carry literal valid
  identifiers, not prose; `artifacts.get`'s own `byte_offset` parameter already
  documents the `next_offset`/`content_truncated` paging contract.
- **Also surface `fetch_raw` for the `debuginfo`/vmcore refs in this change.** A
  correct but separate raw-egress affordance on different refs; out of scope for the
  console-ref discoverability fix.
