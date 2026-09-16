# Pre-gateway tool-catalog assertion survey (#2521)

## Problem

`62fb4fa4d` defaulted `KDIVE_MCP_TOOL_GATEWAY` on, clipping an agent's `tools/list` to the
nine-name `CORE_TOOLS` set (`src/kdive/mcp/exposure.py:264`). PR #2491 fixed the third instance
of an assertion still encoding the superseded flat catalog and named `live_stack` and
`agent_smoke` as unswept; #2522 and #2523 cannot be sized until the rest are enumerated.

## Scope

One point-in-time survey record under `docs/design/`, pinned to this branch's base commit,
enumerating each assertion in the two tiers encoding the pre-gateway catalog. Every entry
carries file:line, the assertion, and one disposition: `fix-needed`, `fix-coupled` (correct only
once the `fix-needed` entry it names changes), or `keep` with a reason.

Surveyed set = the files `pytest -m live_stack|agent_smoke --collect-only` selects, **plus**
every test and support module under the paths #2521 names — neither source alone reaches both.
The record lists every union file with its marker. An issue-named file a marker does not
select is surveyed anyway, under its real marker. `tests/integration/test_wire_harness.py`
reads `excluded (2)`, never `0`.

Each disposition follows one rule: evaluate the assertion's expected catalog against
`CORE_TOOLS` and the agent-profile clip at the base commit. No source or test file changes.

### Failure model

- **Actors** — a reader sizing #2522/#2523; `just lint` (ruff over Python fences); no runtime
  actor and, per Validation, no guardrail over its citations.
- **Invariants** — citations and counts must hold at the named commit; a wrong disposition
  mis-sizes a fix issue.
- **Accepted failure classes** — the record goes stale as `main` moves, bounded by the named base
  commit, which makes the staleness detectable; a hit in a file neither marker nor #2521 names is
  unreachable to this survey, stated rather than implied.
- **Covered elsewhere** — ADR-0268 §4 (`CORE_TOOLS` membership, gateway default); ADR-0268
  Consequences (unadvertised calls not refused); ADR-0042/ADR-0353 (tier admission to
  `just ci`); #2521 (the two PR #2491 tests).

## Success

1. Every file in the surveyed set is listed with its hit count — `0` and `excluded` included.
2. Every entry carries file:line, the assertion, and exactly one disposition.
3. Entries split by tier, each half opening with headline counts; each names its owning fix
   issue, or `unrouted` with the reason when neither #2522 nor #2523 covers its tier.
4. The record names its base commit, the in-flight movers of surveyed files (#2530, #2511),
   and the exact commands reproducing its counts.
5. `git diff --name-only <base>...HEAD` lists only this spec and the record.

## Validation

- **Contract: the record's citations and counts.** Mode: `task-test-not-applicable`. A test over
  citations pinned to one commit goes red when a surveyed file legitimately changes on
  `main` — which is why `check-doc-paths.sh` exempts `docs/design/**`
  (`tests/scripts/test_check_doc_paths.py::test_frozen_design_records_not_scanned`). Each cited
  path:line is instead resolved at the base commit out of band and each count re-derived from
  the record's named command, both reported in the completion report.
- **Contract: each entry's disposition.** Mode: `task-test-not-applicable`. A disposition judges
  that same evidence; the record states the rule above and the completion report gives
  per-disposition counts, so a single entry is re-derivable without re-running it.
