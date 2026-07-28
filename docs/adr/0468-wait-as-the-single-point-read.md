# ADR 0468 — `wait` is the single point-read and polling contract

- **Status:** Accepted
- **Date:** 2026-07-27
- **Issue:** #1592
- **Epic:** #1576
- **Implements:** [ADR-0456](0456-agent-operator-mcp-exposure-profiles.md) §3's retired-name
  search vocabulary, through the mechanism
  [ADR-0458](0458-fold-postmortem-triage-into-postmortem-crash.md) established.
- **Amends:** [ADR-0118](0118-wait-on-resource-mechanisms.md), which added `jobs.wait` and
  `allocations.wait` *beside* the existing getters, and
  [ADR-0414](0414-teardown-surfaces-allocation-release.md)'s generic terminal breadcrumb.

## Context

`jobs.get` and `jobs.wait` are one tool wearing two names, and so are `allocations.get` and
`allocations.wait`.

Both `wait` handlers compute `deadline = loop.time() + min(max(timeout_s, 0.0), MAX_WAIT_S)`
before their poll loop and evaluate `now >= deadline` *after* the first read. A non-positive
`timeout_s` therefore performs exactly one database read and returns — no sleep, no second
query. `wait_job`'s docstring already said so ("A non-positive timeout means a single read");
`wait_allocation` had the same code and no such sentence. The single-read property is not new
behavior this ADR introduces; it is behavior that was already shipped and undocumented on the
allocation side.

Given that, each pair is identical along every axis epic #1576 requirement 5 asks about:

| axis | `jobs.get` / `jobs.wait` | `allocations.get` / `allocations.wait` |
| --- | --- | --- |
| exposure scope | `_VIEWER` both | `_VIEWER` both |
| annotations | `read_only()` both | `read_only()` both |
| maturity | `implemented` both | `implemented` both |
| handler authorization | `require_role(…, VIEWER)` after a no-leak `not_found` | identical |
| response rendering | `_job_response(job)` in both | `envelope_for_allocation(...)` in both |
| execution class | synchronous read | synchronous read |

The duplication is not free. It costs two registry slots against epic #1576's budget, and it
splits the agent-facing story: the tool reference described two ways to learn a job's state
without saying that one is the other with an argument.

The removal is not mechanical, because `visible_next_actions()` **raises `ValueError`** on any
breadcrumb naming an unregistered tool (ADR-0421 decision 3). Every `suggested_next_actions`
list in the tree that named either tool had to move in the same commit or the server would fail
at first render — the two largest being `_NEXT_ACTIONS[SUCCEEDED]` / `[FAILED]` in
`responses.py`, which reach every terminal job envelope, and `allocation_next_actions()`, which
reaches every allocation read, wait, request, and list envelope.

## Decision

### 1. Remove `jobs.get` and `allocations.get`; `wait(timeout_s=0)` is the point read

The two `get` tools are deleted — wrapper and handler both, since `get_job` and
`get_allocation` had no caller outside their own `@app.tool` block and their tests. No alias,
no deprecation shim (repo policy: replace, don't deprecate). The registry goes 125 → 123.

`wait(timeout_s=0)` is the replacement, and it is a strict superset: it returns the same
envelope the getter returned, from the same handler code the getter's tests already covered,
plus the ability to block.

### 2. `timeout_s` keeps its 30-second default; `timeout_s=0` is documented, not defaulted

Changing the default to `0` would make `jobs.wait` a getter and re-break the polling ergonomics
ADR-0118 established. Instead both wrapper docstrings and both `timeout_s` `Field` descriptions
now name `timeout_s=0` as the single-read form in so many words.

The `Field` description and the wrapper docstring are the only text serialized into the tool
schema, so they are the agent-facing contract; a handler docstring is not
(`docs/guide/agents/index.md`). Documenting the single read anywhere else would have left the
capability unreachable in practice.

### 3. `SUCCEEDED` and `FAILED` lose their generic terminal breadcrumb

`_NEXT_ACTIONS[JobState.SUCCEEDED]` and `[JobState.FAILED]` were `["jobs.get"]`. They become
`[]`, matching the `CANCELED: []` precedent already in the same dict.

This is the right answer rather than a mechanical `jobs.get` → `jobs.wait` substitution:
pointing an agent that just *received* a terminal job envelope at a tool that re-reads the same
row is a loop, not a next action. It was a loop before this change too — the breadcrumb told
the caller to call the tool that had just answered. What an agent actually needs after a
terminal job is kind-specific, and that already comes from `_TERMINAL_KIND_ACTIONS`
(ADR-0414): `artifacts.get`, `postmortem.crash`, `allocations.release`. That table is
unchanged and names no removed tool.

`QUEUED` and `RUNNING` keep `["jobs.wait", "jobs.cancel"]` — there, "call `jobs.wait` again" is
a genuine next action, because the row has not settled.

### 4. Both retired names carry their vocabulary to the survivor

`jobs.get` → `jobs.wait` and `allocations.get` → `allocations.wait` join
`RETIRED_TOOL_NAMES` (ADR-0456 §3): search vocabulary only, never callable aliases.

`jobs.get`'s `TOOL_KEYWORDS` entry (`job`, `status`, `get`, `fetch`, `lookup`, `result`) merges
onto `jobs.wait`, and `allocations.wait` gains the getter vocabulary its own entry lacked
(`get`, `status`, `fetch`, `lookup`). Without this, an agent asking the gateway to "get job
status" or "look up an allocation by id" — the exact phrasing the removed tools served — would
match nothing, because the surviving names spell neither `get` nor `status`. With the gateway
now on by default (ADR-0456), that is the difference between a reachable and an unreachable
capability.

### 5. The curated `kdivectl jobs get` / `allocations get` verbs are deleted

`add_subparsers()` emits a curated `Verb` only at a path a *generated* verb already occupies.
With the two tools gone, no generated verb sits at `jobs get` or `allocations get`, so both
curated verbs — and their `reads.py` handlers — became unreachable code and are removed rather
than repointed. This follows the pattern ADR-0461 §"curated verbs" and ADR-0467 set.

The CLI point read is the generated `kdivectl jobs wait <id> --timeout-s 0` and
`kdivectl allocations wait <id> --timeout-s 0`. This is a breaking CLI change, stated plainly
rather than shimmed.

## Consequences

- **Two fewer tools**, 125 → 123. `CORE_TOOLS` is unchanged: `allocations.wait` was already in
  it and neither removed name was, so the gateway's default-listed set does not move and no
  cold-start binding proof is required for this change.
- **No migration.** Neither name is persisted in a schema constraint or a lookup table. Historical
  `platform_audit_log` and `tool_call_trail` rows keep their `jobs.get` / `allocations.get`
  values, which is correct — they record what was actually called at the time. Two pre-existing
  migration files (`0007`, `0038`) mention `jobs.get` in SQL comments; migrations are immutable
  once applied and are not edited.
- **`kdivectl jobs get` / `kdivectl allocations get` stop existing**, with no deprecation period.
- **A new error path reaches point-read callers.** `wait` returns a `configuration_error` for a
  non-finite `timeout_s`; the getters had no timeout to get wrong. A caller passing `0` cannot
  hit it, so this costs nothing in practice, but it is a real difference in the contract and is
  named here rather than glossed.
- **`allocations.wait`'s single-read property is now documented**, where before it was only
  implemented. This ADR is the first place it is written down for allocations.
- **Historical ADRs and dated design records are not rewritten.** ADR-0008, ADR-0026, ADR-0030,
  ADR-0097, ADR-0098, ADR-0105, ADR-0118 and the dated documents under `docs/design/` and
  `docs/archive/` still name the removed tools. They are records of what was decided when, and
  editing them would destroy that. Only living agent-facing documentation — the served
  `resource://` snapshots and their canonical `docs/guide/` sources — is updated, since that is
  what an agent reads as current truth.

## Alternatives considered

- **Default `timeout_s` to `0`.** Rejected by the user: it turns the survivor back into a getter
  and loses ADR-0118's polling default, so every existing caller silently stops waiting.
- **Keep `get` as a thin alias for `wait(timeout_s=0)`.** Rejected: it spends the registry slot
  the epic is trying to reclaim, and repo policy forbids compatibility shims.
- **Substitute `jobs.wait` for `jobs.get` in the terminal breadcrumbs.** Rejected per decision 3:
  it preserves a self-referential loop rather than fixing it.
- **Repoint the curated CLI verbs at `jobs.wait` with a hardcoded `timeout_s=0`.** Rejected: it
  would put a `jobs get` verb and a `jobs wait` verb on the same tool with different implied
  timeouts, which is the duplication this ADR removes, one layer down.
