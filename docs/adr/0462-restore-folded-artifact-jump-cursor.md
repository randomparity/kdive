# ADR 0462 — Restore the folded artifact jump cursor on `artifacts.get`

- **Status:** Accepted
- **Date:** 2026-07-27
- **Issue:** #1583
- **Epic:** #1576
- **Restores:** [ADR-0283](0283-artifact-get-jump-cursor.md), which is still `Accepted` and whose
  contract main had drifted away from.
- **Implements:** [ADR-0456](0456-agent-operator-mcp-exposure-profiles.md) §3's retired-name search
  vocabulary, through the mechanism
  [ADR-0458](0458-fold-postmortem-triage-into-postmortem-crash.md) established.

## Context

This is not a consolidation proposal. ADR-0283 already decided it, and shipped:

- ADR-0283 (Accepted, 2026-06-30) folded literal artifact search into `artifacts.get` as a
  byte-offset jump cursor, deleted `artifacts.search_text`, and — in its *Considered & rejected*
  list — explicitly rejected "keep both tools (filter on `get`, `search_text` retained)" for
  "overlap, agent confusion".
- Commit `c8b5814cd` ("feat(939): byte-space jump matcher for artifacts.get") shipped that folded
  design: one tool, `find` and `direction` on `artifacts.get`.
- Commit `5806f6c0d` ("refactor: split artifact search tool", 2026-07-11) split the matcher back
  out into a standalone `artifacts.find` tool. No ADR amended ADR-0283, which stayed `Accepted`.

Main therefore contradicted the ADR its own code cites. `reads.py` and
`lifecycle/runs/common.py` both referenced ADR-0283 while describing two tools, and the split also
renamed ADR-0283 §1's `find` parameter to `query`. The result is exactly the state ADR-0283
rejected: two `_VIEWER` tools sharing one authorization query, one object load, one redaction
gate, and one response envelope, differing only in which `data` keys the branch fills.

Epic #1576's requirement 5 permits consolidation when authorization, annotations, execution class,
and result shape match. All four matched, which is unsurprising — the split created the divergence
it would otherwise have had to justify:

- authorization — both `_VIEWER`, through the same `_authorized_redacted_artifact` query.
- annotations — both `_docmeta.read_only()`, maturity `implemented`.
- execution class — both `async`, same pool, both loading through `_load_redacted_plaintext`.
- result shape — the same `ToolResponse.success(artifact_id, "available", refs={object,
  download_uri?})` envelope and the same `data.size_bytes`, with the `find`-discriminated `data`
  branch ADR-0283 §2 specifies.

## Decision

### 1. `find` returns to `artifacts.get`, and keeps ADR-0283's name

`ArtifactsGetRequest` gains `find: str | None = None`. `artifacts.find`,
`ArtifactsFindRequest`, and the `artifacts_find` handler are removed in this change — no alias,
no deprecation period. The parameter is `find`, not the shipped `query`: ADR-0283 §1 names it
`find`, the rename came in with the unrecorded split, and restoring the contract means restoring
its vocabulary. `direction` already sat on `artifacts.get` and is unchanged.

`kdive.security.artifacts.artifact_jump` — the byte-space matcher itself — is untouched. This is a
tool-surface change; no schema, migration, RBAC, or config change.

The contract restored is ADR-0283's, in full, not merely "two tools became one":

- `find` absent ⇒ byte-identical to the plain windowed read.
- a hit ⇒ `data.match_found`/`match_offset`/`match_line`/`content`/`next_offset`, with the cursor
  strictly advancing so paging cannot loop or re-emit a boundary match.
- no match in `direction` ⇒ `match_found=false`, no `content`, no `next_offset`.
- `direction="backward"` with an omitted/`0`/negative `byte_offset` ⇒ anchored at end-of-artifact.

### 2. The over-ceiling asymmetry is preserved deliberately

An artifact above the 1 MiB windowed-fetch ceiling behaves differently on the two branches, and
that is the point:

- plain read ⇒ success, `data.content_omitted = "artifact_too_large"`, plus `refs.download_uri`.
- `find` ⇒ `configuration_error`, `data.reason = "artifact_too_large"`.

`find` cannot search bytes that were never fetched. Collapsing the branches onto the plain
behavior would return `match_found=false` for a log that was never read, letting "could not
search" be read as "no such crash" — the failure ADR-0283's Consequences legislate against. The
same reasoning already governs a store outage, where neither branch emits `match_found` at all.
The asymmetry is keyed on `find` presence and each branch is pinned by its own test.

### 3. The retired name and its intent vocabulary both stay discoverable

Neither tool was ever in `CORE_TOOLS`, so with the gateway now on by default (ADR-0456) both were
reachable only through `tools.search`. Removing `artifacts.find` without moving its search terms
would have made artifact text search *unreachable* — the code present, the intent unfindable.

Two changes prevent that. `RETIRED_TOOL_NAMES` gains `"artifacts.find": "artifacts.get"`, per
ADR-0456 §3 and the ADR-0458 mechanism. And `artifacts.get`'s `TOOL_KEYWORDS` entry absorbs the
retired tool's vocabulary — `search`, `find`, `text` — plus `grep`, `string`, and `match`, so an
agent that describes the intent rather than naming a tool ("search text in a log", "find a string
in console output") still ranks `artifacts.get`. Retired names remain discovery vocabulary only;
`tools.invoke("artifacts.find")` returns the usual unknown-tool `configuration_error`.

The `runs.get` `data.console_access` hint carried `"search": "artifacts.find"` in a plain data
dict, which the `visible_next_actions` guard does not inspect, so a stale name there would have
shipped silently. It now reads `"artifacts.get(find=...)"`, spelling the parameter rather than a
second tool name, because the jump matcher is no longer separately callable.

## Consequences

- The live registry drops from 137 tools to 136.
- Breaking for `artifacts.find` callers, twice over: the tool is gone and the parameter is renamed
  `query` → `find`. ADR-0283 accepted this class of break as pre-first-release, and the epic
  forbids aliases.
- Not breaking for existing `artifacts.get` callers. With `find` absent the response is byte-for-
  byte what it was.
- `kdivectl artifacts find` becomes `kdivectl artifacts get --find ...`. The verb descriptors are
  regenerated, never hand-edited.
- The one-match-per-call cost ADR-0283 accepted returns with the design: enumerating N scattered
  matches is N stateless calls, each re-fetching and re-scanning the ≤1 MiB body.
- ADR-0283 is **not** edited. It was correct and remains ratified; this ADR records the drift and
  the restoration.

## Rejected alternatives

- **Amend ADR-0283 in place to describe two tools.** That would ratify the drift after the fact and
  destroy the record that a ratified decision was reversed by an untracked refactor.
- **Keep `artifacts.find` and add `find` to `artifacts.get` too.** Two overlapping surfaces for one
  capability — the option ADR-0283 named and rejected, and the state this ADR exists to undo.
- **Keep the shipped `query` parameter name to reduce churn.** The break is already unavoidable for
  every `artifacts.find` caller, so preserving the drifted name buys nothing and leaves the code
  disagreeing with the ADR it cites.
- **Collapse the over-ceiling asymmetry so both branches return the plain `content_omitted`
  success.** It reads as a simplification and is a correctness regression: a failed search would be
  indistinguishable from a clean log.
- **Move only `search`/`find`/`text` onto `artifacts.get` and skip `RETIRED_TOOL_NAMES`.** An agent
  that knows the old tool name by heart would get zero hits, which is the case the retired-name
  mechanism exists for.
