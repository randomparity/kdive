# ADR 0459 — Replace queue pause/resume with one state setter

- **Status:** Accepted
- **Date:** 2026-07-27
- **Issue:** #1586
- **Epic:** #1576
- **Implements:** [ADR-0456](0456-agent-operator-mcp-exposure-profiles.md) §3's retired-name search
  vocabulary, through the mechanism [ADR-0458](0458-fold-postmortem-triage-into-postmortem-crash.md)
  established.
- **Amends:** [ADR-0062](0062-platform-operations.md)'s two-tool queue-control surface.

## Context

`ops.queue_pause` and `ops.queue_resume` were two names for one write. Both delegated to the same
private `_set_paused` helper, differing only in the boolean it passed and the tool name it recorded.
Epic #1576's requirement 5 permits consolidation when authorization, annotations, execution class,
and result shape all match; all four did:

- authorization — one shared `require_platform_role(ctx, PLATFORM_OPERATOR)` call, and the same
  `_PLAT_OP` exposure classification. Neither direction is more privileged than the other, so there
  is no branch-dependent authorization to make explicit.
- annotations — both `_docmeta.mutating()` (`readOnlyHint=False`, `destructiveHint=False`), maturity
  `implemented`.
- execution class — both synchronous, returning a `ToolResponse` rather than a job handle.
- result shape — one shared return site: `data={"queue_paused": bool}` with
  `suggested_next_actions=["ops.jobs_list"]`. Only the status literal differed, `paused` vs
  `running`.

The underlying state is a plain `BOOLEAN` column, `queue_paused` on the singleton `ops_control` row
(`0011_ops_control.sql`), which the worker reads before each `dequeue`. It is already a
target-state write; nothing about the storage needs to change.

## Decision

### 1. One tool that writes the target state

`ops.set_queue_paused(paused: bool)` replaces both wrappers. `paused` is a flat top-level parameter,
as ADR-0372 requires of every mutation tool. `kdive.jobs.queue.set_queue_paused` is reused unchanged:
no DDL, no migration.

The tool writes a target state rather than toggling, so repeating a call is idempotent — the flag
ends at `paused` whatever it held before. Every accepted call still audits, including a no-op
re-assert, because `platform_audit_log` records the operator's intent, not the observed transition.
The flag flip and its audit row keep their shared transaction, so a failed audit write can never
leave the queue paused unrecorded.

One consequence is worth stating plainly: the audit trail's `tool` column no longer distinguishes a
pause from a resume — both rows read `ops.set_queue_paused`. The direction survives in the recorded
arguments, which is where the audit reader must now look, and a test pins that the two directions
produce different `args_digest` values.

`ops.queue_pause` and `ops.queue_resume` are removed in this change, with no alias and no
deprecation period. The `ops.jobs_list` success envelope, which suggested both names, now suggests
the one.

### 2. A required boolean needs `--flag` / `--no-flag`

The generated `kdivectl` verb exposed a defect the old surface hid. `scripts/gen_cli_verbs.py`
mapped every boolean parameter to argparse `store_true`, whose only two states are "flag given"
(true) and "flag absent". That is correct for an *optional* boolean, where absence must stay
distinguishable from an explicit false so the server default holds. For a *required* boolean it is
not expressible: `kdivectl ops set-queue-paused` could pause the queue and never resume it.

A required boolean therefore generates the `bool_optional` action — argparse's
`BooleanOptionalAction`, giving `--paused` and `--no-paused` — while optional booleans keep
`store_true`. The split is deliberate rather than a blanket switch: applying
`BooleanOptionalAction` everywhere would give each existing optional boolean a `--no-` form that
sends an explicit `false` where the tool schema expects the key omitted. `ops.set_queue_paused` is
currently the only required boolean in the catalog, so no other generated verb changes.

### 3. Both retired names as search vocabulary

`RETIRED_TOOL_NAMES` gains two rows, both pointing at the replacement:

```python
"ops.queue_pause": "ops.set_queue_paused",
"ops.queue_resume": "ops.set_queue_paused",
```

The inversion ADR-0458 built groups them under the one replacement, so `tools.search` scores both
against `ops.set_queue_paused` in a single lookup. This matters more here than for a rename: the
replacement's own name contains `pause`, but nothing in it spells `resume`, so without the retired
vocabulary an agent searching for the resume direction would find nothing.

Retired names remain discovery vocabulary only. `tools.invoke("ops.queue_pause")` returns the usual
unknown-tool `configuration_error`.

## Consequences

- The live registry drops from 139 tools to 138.
- `kdivectl ops queue-pause` and `ops queue-resume` become `ops set-queue-paused --paused` and
  `--no-paused`. The verb descriptors are regenerated, never hand-edited.
- An audit reader that filtered on `tool = 'ops.queue_pause'` must filter on the recorded arguments
  instead. The trail is pre-release and no shipped consumer does this.
- Any future required boolean parameter inherits the `--flag` / `--no-flag` pair automatically.
- The worker, the `ops_control` row, and `kdive.jobs.queue` are untouched; this is a tool-surface
  change only.

## Rejected alternatives

- **A `state` enum of `"paused"` / `"running"`.** It invents a second vocabulary for a column that
  is a boolean, and every caller would have to map back to the boolean the tool writes anyway.
- **Keeping the two names as aliases onto the setter.** The project is pre-release and follows
  replace-don't-deprecate; two names would persist in the catalog, the RBAC matrix, the generated
  CLI, and the served docs for no capability.
- **A toggle with no argument.** It is not idempotent: a retried call after an ambiguous failure
  inverts the state the operator wanted, which is the worst behavior for a control that freezes
  work.
- **Switching every boolean flag to `BooleanOptionalAction`.** It would let an optional boolean send
  an explicit `false` where the tool contract expects the key omitted, changing existing verbs to
  fix a case none of them have.
- **Auditing only real transitions, skipping the no-op re-assert.** The audit trail would then
  record what the flag did rather than what the operator asked for, and a re-assert issued because
  the first call's outcome was unclear is exactly the event an operator most wants recorded.
