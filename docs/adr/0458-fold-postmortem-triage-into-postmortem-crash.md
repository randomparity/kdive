# ADR 0458 — Fold postmortem triage into `postmortem.crash`

- **Status:** Accepted
- **Date:** 2026-07-27
- **Issue:** #1585
- **Epic:** #1576
- **Implements:** [ADR-0456](0456-agent-operator-mcp-exposure-profiles.md) §3's retired-name
  search vocabulary, as the shared mechanism every consolidation under #1576 uses.
- **Amends:** [ADR-0031](0031-retrieve-plane-vmcore-postmortem.md)'s two-tool postmortem surface.

## Context

`postmortem.triage` was `postmortem.crash` with the command list pinned to `log`, `bt` and the
success envelope's `suggested_next_actions` relabelled. Both tools shared the same authorization
(contributor), the same MCP annotations (`readOnlyHint`, maturity `implemented`), the same
execution class (synchronous, returning a `ToolResponse` rather than a job handle), and the same
result shape (`postmortem_success_response`). Epic #1576's requirement 5 permits consolidation
exactly when those four match.

Removing a tool name is only safe if an agent that knows the old name can still find the new one.
ADR-0456 §3 requires retired names to remain `tools.search` vocabulary pointing at the replacement,
with no compatibility alias. `TOOL_KEYWORDS` cannot carry them: its completeness guard asserts that
every key is a live registered tool, which is the property that keeps the index from accumulating
stale entries.

## Decision

### 1. One postmortem tool with an optional command list

`postmortem.crash` takes `commands: list[str] | None`. When omitted it runs the standard first-pass
batch — `log`, `bt`, the former triage batch, exported as `DEFAULT_CRASH_COMMANDS` — and otherwise
runs the caller's allowlisted commands unchanged. There is no `preset` enum: a preset name would be
a second vocabulary for the one default the tool already has.

`postmortem.triage` is removed in this change, along with `triage_response`, the relabelling helper
that existed only to serve it. A successful call now always returns
`suggested_next_actions = ["postmortem.crash", "artifacts.list"]`, whichever command list ran.

The allowlist, authorization, redaction, console-crash redirect, and error contract are untouched:
the default batch reaches exactly the same handler path as an explicit list, so it is validated
against `CRASH_COMMAND_ALLOWLIST` and redacted before return like any other command.

Making `commands` optional shifts the generated `kdivectl` verb's arity; the committed verb
descriptors are regenerated, never hand-edited.

### 2. Retired tool names as curated search vocabulary

`RETIRED_TOOL_NAMES` in `kdive.mcp.schema.tool_index` maps each removed tool name to the live tool
that replaced it:

```python
RETIRED_TOOL_NAMES: dict[str, str] = {
    "postmortem.triage": "postmortem.crash",
}
```

It is inverted once at import into `_RETIRED_BY_REPLACEMENT` (replacement → retired names) and read
through `retired_names_for(tool_name)`, so `tools.search` scoring stays one dictionary lookup per
candidate tool instead of a rescan of the whole map. `_score` folds those names into the same
lowercased haystack it already builds from the tool's name, description, `TOOL_KEYWORDS` extras,
and bounded schema text. Because scoring counts substring hits, storing the dotted name makes
`postmortem.triage`, `triage`, and `postmortem` all rank `postmortem.crash`.

Retired names are discovery vocabulary only. `tools.invoke("postmortem.triage")` returns the usual
unknown-tool `configuration_error`; nothing dispatches on the old name.

A guard asserts, for every entry, that the key is absent from the live registry and the value is
present. The absent-key half is the one that catches a consolidation that added the search
vocabulary but never removed the wrapper.

## Consequences

- The live registry drops from 140 tools to 139.
- Every later consolidation under #1576 adds one row to `RETIRED_TOOL_NAMES` and inherits both the
  guard and the parametrised `tools.search` behaviour test; the gateway needs no further change.
- An agent that omits `commands` gets the former triage behaviour, so the first-pass path costs one
  tool call with no argument, as before.
- Callers of the removed name get an unknown-tool error rather than a redirect. `tools.search`
  is the recovery path, which is why the vocabulary guard is a hard gate rather than advice.
- The next-action graph loses a node: the breadcrumbs in `runs.get`'s expected-crash branch, the
  `triage_panic` prompt chain, and the vmcore response envelopes all name `postmortem.crash`.

## Rejected alternatives

- **A `preset="triage"` enum argument.** It adds a second name for the default the tool already
  has, and every future preset would need its own schema value, doc line, and generated verb arity.
- **Keeping `postmortem.triage` as an alias that forwards to `postmortem.crash`.** The project is
  pre-release and follows replace-don't-deprecate; an alias keeps two names in the catalog, the
  RBAC matrix, the generated CLI, and the served docs for no capability.
- **Adding retired names as `TOOL_KEYWORDS` keys.** The keys are the live-tool completeness guard;
  admitting dead names there would delete the invariant that keeps the index honest.
- **Computing the inverted map on each `_score` call.** Scoring runs once per candidate per search;
  a per-call rescan of `RETIRED_TOOL_NAMES` grows with every consolidation in the epic.
