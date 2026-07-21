# ADR 0407 — Guard the agent-index golden path as a directed graph

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-21
- **Deciders:** David Christensen

## Context

The served `agent-index.md` (`resources/_content/agent-index.md`) is the entry point an
agent reads to drive an investigation. Its "typical session" section encodes a canonical
journey: an ordered set of stages from orientation through wind-down, each naming the tools
to call. Recent agent-surface work repaired that journey — #1362 gave the discovery stage its
full fan-out (`session.whoami`/`resources.list`/`resources.availability`/`shapes.list`/
`accounting.estimate`) and named the wind-down order (`systems.teardown` →
`allocations.release` → `investigations.close`), and the P1 batch (#1361) removed
self-referential next-action loops from discovery/catalog tools. Nothing guards those
repairs: a later doc edit could silently drop a discovery tool, reorder the wind-down, rename
a referenced tool, or make a stage point back at itself, and no test would notice.

#1366 asked for a guard "over the live registry using the `list_tools()` harness" asserting
(1) no tool's `suggested_next_actions` is a pure self-loop and (2) the golden-path edges hold.
Verifying the surface first surfaced two false premises in that framing that bound this
decision:

1. **`suggested_next_actions` values are not exposed by `list_tools()`.** The advertised
   output schema (`schema_advertising.py`) declares the field with a generic
   `array-of-string` shape; the actual next-action *values* are constructed inside each
   handler's `ToolResponse` at call time and never appear on the `Tool` schema. Building a
   per-tool next-action graph from `list_tools()` is therefore impossible.
2. **A blanket "no pure self-loop" rule is wrong.** The clean registry has intentional
   retry/re-poll self-loops — `accounting.estimate`, `accounting.usage_project`,
   `accounting.usage_investigation`, `artifacts.fetch_raw`, `fixtures.validate`, and the
   documented `jobs.wait` "still running, call again" loop. Asserting no tool ever self-loops
   would fail on a clean tree. The P1-6 fix targeted only discovery/catalog *success-path*
   self-loops, a narrow subset — not a global invariant.

## Decision

We will guard the *doc-encoded* golden path, not per-tool handler next-actions. A pytest
module (`tests/mcp/core/test_next_actions_graph.py`) encodes the golden path from
`agent-index.md` as reviewed data (one entry per stage: its primary tool and the tools it
must name), parses the served snapshot the server actually hands to agents, and asserts:
every graph node resolves to a live tool (validated against the `list_tools()` registry); the
doc's per-stage primary sequence equals the reviewed path and is all-distinct (no stage is
self-referential); each stage names its required tools; `images.describe` reaches
`runs.create` along the path; and the wind-down stage names teardown → release → close in
order. The registry is where `list_tools()` is authoritative — for node existence, not for
edges.

## Consequences

- A doc edit that drops a golden-path tool, reorders the wind-down, renames a referenced
  tool, or makes a stage self-loop fails the guard; the #1362/#1361 repairs cannot silently
  regress. Verified by mutation (each failure mode was reproduced and observed to fail; the
  clean tree passes).
- **Known, intentional gap:** code-level handler `suggested_next_actions` self-loops are not
  guarded here, because those values are runtime-constructed and absent from the `Tool`
  schema (premise 1). Catching them would require invoking every handler with live services —
  out of proportion for this guard, and not what `list_tools()` can support.
- The golden-path data is a hand-reviewed table that must be updated in lockstep when the
  journey deliberately changes — the same drift-guard shape as the existing
  `_EXPECTED_STEP_MATURITY` table in `test_app.py`. This is the intended cost: a deliberate
  journey change is a reviewed event.
- Complementary to #1365, which guards node validity generically (every next-action literal,
  doc backtick, and prompt step resolves to a live tool). This ADR's guard covers the
  golden-path *edge* structure; the two do not overlap.

## Alternatives considered

- **Build the per-tool `suggested_next_actions` graph from `list_tools()`** — impossible; the
  values are not on the tool schema (premise 1).
- **Static-scan handler source for `suggested_next_actions=[self]`** — brittle (resolving a
  literal to its registered tool name across module constants, f-strings, and locals) and
  would flag the intentional retry self-loops (premise 2); it tests implementation text, not
  behavior.
- **Invoke every tool and read the runtime envelope** — needs full services and per-tool
  arguments; far too heavy for a registry-wide guard, and most edges only appear on specific
  success/error branches.
- **Close #1366 as invalid-premise (as #1354 was)** — rejected by the orchestrator: the
  issue's core intent ("encode the golden path from agent-index.md as data" and guard the
  repairs) is fully buildable and valuable; only the two sub-claims were wrong, and they are
  scoped out here rather than sinking the guard.
