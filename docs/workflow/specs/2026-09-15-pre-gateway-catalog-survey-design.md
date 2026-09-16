# Pre-gateway tool-catalog assertion survey (#2521)

## Problem

`62fb4fa4d` defaulted `KDIVE_MCP_TOOL_GATEWAY` on, clipping an agent's `tools/list` to the
nine-name `CORE_TOOLS` set (`src/kdive/mcp/exposure.py:264`). PR #2491 fixed the third instance
of assertions still written against the superseded flat catalog and named the `live_stack` and
`agent_smoke` tiers as unswept. #2522 and #2523 cannot be sized until those hits are enumerated.

## Scope

One point-in-time survey record under `docs/design/`, pinned to this branch's base commit,
enumerating each assertion in the two tiers that encodes the pre-gateway catalog. Every entry
carries file:line, the assertion, and one disposition: `fix-needed`, `fix-coupled` (correct only
once a `fix-needed` entry changes), or `keep` with a reason. Entries split by tier so each half
sizes one fix issue.

Tier membership is decided by `pytest -m <marker> --collect-only`, not by the file list in #2521.
A file the issue names that the marker does not select is recorded as a scope correction instead
of surveyed as a member; a member the issue omits is surveyed anyway.

No source or test file changes: the fixes belong to #2522 (`live_stack`) and #2523
(`agent_smoke`).

### Failure model

- **Actors and deployments** — a reader sizing #2522/#2523; the doc guardrails `just lint` and
  `just type`. No runtime actor; the record ships no executable code.
- **Invariants at stake** — the record's file:line citations and its counts must hold at the
  named commit. A wrong disposition mis-sizes a downstream fix issue.
- **Accepted failure classes** —
  - the record goes stale as `main` moves: bounded by the named base commit, which is what
    makes the staleness detectable;
  - a hit reachable only under a runtime profile no surveyed test exercises: not reachable in
    the surveyed deployments;
  - `tests/integration/test_wire_harness.py`'s two PR #2491 tests: excluded by #2521.
- **Covered elsewhere** — `CORE_TOOLS` membership and the gateway default: ADR-0268 §4. Tier
  admission to `just ci`: ADR-0042/ADR-0353. Unadvertised-tool calls not refused: ADR-0268
  Consequences.

## Success

1. Every file the two markers select is listed in the record with its hit count, zero included.
2. Every entry carries file:line, the assertion, and exactly one disposition.
3. Entries are split by tier, each half opening with its headline counts.
4. The record names its base commit and the exact commands reproducing its counts.
5. `git diff --name-only <base>...HEAD` lists only this spec and the record.

## Validation

- **Contract: the survey record's citations and counts.** Mode: `task-test-not-applicable`.
  The record is point-in-time evidence pinned to one commit; a repository test over its
  citations would go red the moment a surveyed file legitimately changes on `main`, inverting
  what the record is for, and no record under `docs/design/` carries one. Instead each cited
  path:line is resolved against the base commit out of band, and each count re-derived from the
  command the record names, with both results reported in the completion report.
