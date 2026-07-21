# Doc-driven agent smoke — harness + first green pass (#1370)

Status: Draft
Issue: #1370
ADR: [0411](../adr/0411-doc-driven-agent-smoke-harness.md)

## Problem

Static doc guards (#1361–#1369) catch docs that are internally inconsistent, but not a doc
that is internally consistent yet *semantically wrong* against the surface the server hands
an agent: `agent-index.md` names a tool the registry no longer has, links a toolset guide no
longer served, or advertises a prompt that no longer renders. An agent driving the golden
path hits a **stall** — a step whose next action is a dead end.

Epic #1360's seam is a non-PR-gate job where an agent drives the golden path using only the
served surface and fails on any stall, paralleling the `live_vm` / `live_stack` gated tiers.
This issue's scope is **the harness plus one green pass**; the nightly schedule, runner, and
credentials are deferred to the epic.

## Goals

- A runnable harness that drives the served MCP surface along the `agent-index.md` golden
  path and reports every stall.
- One green golden-path pass against the served surface, gated as a non-PR tier.
- No new dependency (no LLM SDK); no live VM/stack required.

## Non-goals (deferred to epic #1360)

- The standing nightly schedule, its runner, and its credentials.
- A true live-LLM agent driver (the scripted walker is its harness/placeholder).
- Deciding whether the live version reuses the `live_vm` stack or a lighter harness (this
  delivers the lighter served-surface harness).

## Design

A deterministic **scripted walker** (`tests/smoke/agent_smoke/walker.py`) is given a built
app and drives the served surface via `read_resource`, `list_tools`, `list_prompts`, and
`render_prompt`. It parses the served `agent-index.md` at runtime and follows what the doc
advertises, returning a `WalkResult` of `Stall(stage, reason)`.

Stall conditions:

1. The entry doc is not served or reads back empty.
2. A numbered "typical session" stage names no live tool.
3. The wind-down stage cannot reach `systems.teardown` / `allocations.release` /
   `investigations.close` live.
4. A `resource://kdive/...` link the served index advertises is not served or reads empty
   (trailing sentence punctuation stripped before resolution).
5. The always-available gateway (`tools.search` / `tools.invoke`) is absent.
6. A named lifecycle prompt (`start_investigation`, `build_boot_debug`, `triage_panic`) is
   not listed or does not render.

The gated test (`tests/smoke/agent_smoke/test_agent_golden_path.py`, marker `agent_smoke`)
builds the app, walks, and asserts `WalkResult.stalls == []`.

## Relationship to #1366 (ADR-0407)

#1366 is a **PR-gate static graph guard** over a reviewed node/edge table (text snapshot).
This is a **gated non-PR served-surface walk** (opens each linked resource, renders each
prompt, reports the first dead end). Complementary failure classes; the walker holds no
reviewed tool table, so nothing must stay in lockstep with #1366.

## Gating

- `agent_smoke` marker registered in `pyproject.toml`.
- `just test-agent-smoke` recipe (`--strict-markers`, skips cleanly if no tests collected).
- Default `just test` selection excludes it (`and not agent_smoke`).
- Not in `ci:` and not a required PR check.

## Testing

- Walker unit behavior: the clean served surface walks with zero stalls; each stall class is
  injected (monkeypatched surface) and observed to fire — mutation-verifies the guard.
- The one green pass: `just test-agent-smoke`.
