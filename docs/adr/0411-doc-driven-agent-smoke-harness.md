# ADR 0411 — Doc-driven agent-smoke walker over the served surface

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-21
- **Deciders:** David Christensen

## Context

The one drift class the static doc guards cannot catch is a doc that is internally
consistent but *semantically wrong* against the surface an agent actually receives: the
served `agent-index.md` names a tool that is no longer registered, links a toolset guide
that is no longer served, or advertises a lifecycle prompt that no longer renders. Each is a
**stall** — an agent driving the golden path reaches a step whose next action is a dead end.

Epic #1360's seam (#1370) is "a non-PR-gate job where an agent drives the golden path
(`agent-index.md`) using only the served surface and fails on any stall," paralleling the
existing `live_vm` / `live_stack` gated tiers. The scope for *this* issue is deliberately
narrow: **build the harness and land one green golden-path pass.** The standing nightly
schedule, its runner, and its credentials are explicitly deferred to the epic and do not
gate closure.

Two constraints bound the design:

1. **No LLM infrastructure exists.** The repo has no Anthropic/LLM SDK dependency and no
   API-key plumbing in `src/kdive`. A true live-LLM nightly agent is the deferred follow-up;
   introducing an LLM SDK now would be an unjustified dependency the codebase rejects.
2. **A sibling guard already covers the graph statically.** #1366 (ADR-0407,
   `test_next_actions_graph.py`) is a **PR-gate** static guard: it encodes the golden-path
   stages as reviewed data and asserts the doc's graph *nodes/edges* resolve to live
   `list_tools()` tools, the wind-down order holds, and `images.describe` reaches
   `runs.create`. It reads the `_content` snapshot as text and never opens a resource or a
   prompt.

## Decision

We build a **deterministic scripted golden-path walker** — not a live-LLM agent — as the
harness for the deferred nightly smoke, and land one green pass.

The walker (`tests/smoke/agent_smoke/walker.py`) is given a built app and drives the
**served surface** exactly as an agent would reach it — `read_resource`, `list_tools`,
`list_prompts`, `render_prompt` — and returns a `WalkResult` listing every **stall**. The
gated test (`tests/smoke/agent_smoke/test_agent_golden_path.py`, marker `agent_smoke`)
builds the app, walks, and asserts zero stalls.

The walk is **surface-driven**: it parses the served `agent-index.md` at runtime and follows
what the doc itself advertises, so it needs no hand-copied tool table of its own. A stall is
recorded when:

- the entry doc (`resource://kdive/docs/guide/agent-index.md`) is not served or reads back
  empty (the agent cannot even orient);
- a numbered "typical session" stage names **no** live tool (its whole action set was
  renamed or removed — the agent has no next action at that stage);
- the wind-down stage cannot reach `systems.teardown` / `allocations.release` /
  `investigations.close` as live tools (the agent cannot release what it acquired);
- a `resource://kdive/...` link the served index advertises is not served or reads back
  empty (the agent opens a linked guide and gets a dead end);
- the always-available gateway (`tools.search` / `tools.invoke`) is absent (a lazy-loading
  host's only fallback is gone);
- a lifecycle prompt the index names (`start_investigation`, `build_boot_debug`,
  `triage_panic`) is not listed or does not render.

Gating mirrors the live tiers: a `agent_smoke` pytest marker, a `just test-agent-smoke`
recipe, exclusion from the default `just test` selection, and **no** entry in the `ci:`
umbrella or any required PR check. The one green pass is `just test-agent-smoke`.

## Consequences

- The harness exists and completes one green served-surface walk today; a future edit that
  strips a golden-path tool, un-serves a linked guide, or breaks a prompt fails it with a
  named stall. Verified by mutation (each stall class was injected and observed to fire; the
  clean tree walks green).
- **Complementary to #1366, not duplicative.** ADR-0407 statically guards the golden-path
  *graph* (reviewed node/edge table, ordering) as a PR gate; this ADR *walks the served
  surface* (opens each linked resource, renders each prompt, reports the first dead end) as a
  gated non-PR smoke. The two failure classes do not overlap: #1366 catches "the doc
  regressed against reviewed intent"; #1370 catches "the doc names surface the server does
  not actually serve." The walker reuses no reviewed tool table — it reads whatever the
  served doc advertises — so there is nothing to keep in lockstep with #1366.
- **The deterministic walker is infra-free** (built app over a closed pool + the dummy
  `KDIVE_S3_*` test env; no DB, S3, VM, or network) and *could* run in the PR gate. It is
  deliberately kept in the `agent_smoke` tier instead, so the deferred nightly's true
  live-LLM agent can replace the walker within the **same** tier without abruptly turning a
  network-and-credential-dependent smoke into a required PR check.
- **Deferred, by issue scope:** the standing nightly schedule, its runner, and its
  credentials, and the open question of whether the live version reuses the `live_vm` stack
  or a lighter served-surface harness. This ADR delivers the lighter served-surface harness;
  the runner/credentials survive to epic #1360.

## Alternatives considered

- **A live-LLM agent driving the surface now** — rejected: no LLM SDK or API-key plumbing
  exists, and adding one is explicitly deferred and an unjustified dependency today. The
  scripted walker satisfies the issue's literal ask ("agent drives golden path… fails on any
  stall") and matches its own "lighter served-surface-only harness" open question.
- **Fold the walk into #1366's PR-gate test** — rejected: the issue asks for a non-PR-gate
  tier that parallels `live_vm`/`live_stack` and becomes the nightly agent's harness; and the
  walk's served-surface reads (resources, prompts) are a different exercise than #1366's
  static graph assertion over the text snapshot.
- **Assert every tool-shaped backtick in the doc resolves** — rejected: many backticked
  tokens are refs (`refs.latest_console`), response fields (`data.supports_snapshots`), or
  provider paths (`provider.local-libvirt.debug.gdbstub`) that are *not* tools; asserting the
  converse would false-stall. The walk instead requires each stage to name **at least one**
  live tool, which is the real "the agent has an action here" invariant.
