# 0404 — Disambiguate the P2 agent-surface contracts

Status: Accepted

- **Date:** 2026-07-21
- **Issue:** #1362 (P2 agent-surface fixes), parent epic #1360.
- **Relates to:** [ADR-0270](0270-no-adr-refs-in-agent-surface.md) (docstrings and
  `Field` text are the agent's entire contract — no ADR refs on that surface) and
  [ADR-0403](0403-serve-cross-referenced-agent-docs.md) (the P1 sibling batch on the
  same surface).

## Context

The agent-doc review turned up eight correctness-affecting gaps in the tool surface an
agent reads to drive an investigation. Unlike the P1 batch (unreachable doc links), these
are cases where the *text* is present but underspecifies a contract, so an agent cannot
choose between near-identical tools or know an operation's blast radius:

- Two report families — `accounting.report_*` (inline spend rollup) and
  `reports.generate_*` (multi-section CSV/XLSX export behind presigned URLs) — had
  docstrings that named neither output shape, so they read as duplicates.
- Operator tools (`images.delete`/`prune_expired`, `resources.deregister`, the reconcile
  pair) stated neither the required role nor, for the destructive ones, that the effect is
  irreversible.
- `ops.reconcile_now` and `ops.reconcile_systems` were mutually silent, and the latter's
  permanent prune was understated.
- `KCU` — the unit every accounting number is denominated in — was used across the surface
  with no definition anywhere an agent reads.
- The `agent-index` session walkthrough named neither the discovery stage tools an agent
  starts from nor the full wind-down (only `allocations.release`, not `investigations.close`
  / `systems.teardown`).
- `fixtures.list` did not say fixtures are the public baseline subset of images.
- `runs.boot`'s `install_first` rejection returned no `suggested_next_actions`, unlike its
  sibling unbound-run rejection which points at `runs.bind`.

## Decision

**Make each contract self-describing on the surface the agent actually reads** —
docstrings, `Field` descriptions, and the two served `agent-index` copies — and add the
one missing next-action hint:

- Name the **output shape** in both report families' docstrings (inline JSON rollup vs
  CSV/XLSX spreadsheets + presigned `refs` URLs) so the two are distinguishable.
- State the **gate (role) and irreversibility** on the operator tools; cross-reference the
  reconcile pair and spell out what `ops.reconcile_systems` permanently prunes.
- **Define KCU once** in the `accounting.estimate` docstring (the natural first read):
  the dimensionless cost unit, `cost = size × time`, 1 vCPU-hour = 1.0 KCU, 1 GB-hour =
  0.25 KCU, scaled by cost class.
- In `agent-index`, name the **discovery-stage tools** (`session.whoami`, `resources.list`,
  `availability`, `shapes.list`, `accounting.estimate`) and the **full wind-down order**
  (`investigations.close` → `allocations.release` → `systems.teardown`).
- Cross-link `fixtures.list` to images and state fixtures are the public baseline subset.
- Add `suggested_next_actions=["runs.install"]` to `runs.boot`'s `install_first` rejection,
  mirroring the sibling unbound path.

## Consequences

- Seven items are pure text on the agent surface — no behavior change. The `agent-index`
  edits regenerate their served snapshot via `just resources-docs` and are drift-guarded.
- One behavioral change: the `runs.boot` `install_first` error now carries an actionable
  next step. A regression test asserts the hint and the existing `reason` are both present.
- No new tool, no schema or migration change.

## Considered & rejected

- **A shared "reporting" concept doc instead of per-tool docstrings.** Rejected: an agent
  reads the tool's own description at call time (ADR-0270); a separate doc it may not fetch
  does not disambiguate the two tools at the point of choice.
- **Making `install_first` reuse the generic `config_error` unchanged.** Rejected: the
  sibling unbound path already sets `suggested_next_actions`, so the asymmetry was a gap,
  not a deliberate contract.
