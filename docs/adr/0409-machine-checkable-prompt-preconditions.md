# 0409 — Machine-checkable prompt step preconditions

Status: Accepted

- **Date:** 2026-07-21
- **Issue:** #1369 (guard: machine-checkable prompt preconditions), parent epic #1360.
- **Relates to:** [ADR-0202](0202-mcp-lifecycle-prompts.md) (the canonical lifecycle
  prompts this guards) and [ADR-0404](0404-p2-agent-surface-contract-disambiguation.md)
  (the P2 batch that added the `runs.boot` `install_first` next-action hint).

## Context

The three canonical lifecycle prompts (ADR-0202) are ordered tool sequences. A step's
precondition — "`introspect.run` needs a live drgn-live session, which only
`debug.start_session` provides" — lived only in `summary`/`purpose` prose. Prose is not
checkable: reorder `introspect.run` ahead of `debug.start_session`, or drop the
session-opening step, and nothing fails. An agent then stalls partway through the journey
with no session (the `build_boot_debug` P1-4 stall).

The registrar already fails fast at registration on an unknown or `planned` referenced
tool, but not on a broken *ordering*.

## Decision

**Model each step's precondition as machine-checkable capability tags, not prose.** `Step`
gains two fields, `requires: tuple[str, ...]` and `provides: tuple[str, ...]`. A
`_validate_preconditions` walk over each `PromptSpec.steps` accumulates the capabilities
provided so far and raises `RuntimeError` the first time a step `requires` a capability no
*earlier* step has provided. `register` runs the walk before rendering, so a mis-ordered
journey is rejected at registration, and a test asserts every canonical journey passes plus
that a deliberately-broken fixture fails.

The capability vocabulary is minimal and grounded in the existing journeys — each tag
names a real artifact the sequence produces and consumes (`run` → `built-run` →
`installed-kernel` → `booted-system` → `drgn-live-session`; `crash` → `vmcore`;
`resource` → `allocation` → `granted-allocation` → `system`).

Capabilities are **journey-local**. A step's *cross-journey* prerequisite (an open
investigation, or a booted system carried over from an earlier prompt) stays in `summary`
prose and is intentionally not modelled, so every journey's first step carries no
`requires` and the guard never fails a legitimate opening step.

## Consequences

- The `build_boot_debug` stall is caught structurally: dropping or reordering
  `debug.start_session` before `introspect.run` now raises, verified by a mutation check.
- The vocabulary is documentation, not enforcement of real runtime state — a tag asserts
  the *journey* orders its steps coherently, not that a live session exists. Cross-journey
  preconditions remain prose (a deliberate scope boundary).
- Prompt authors adding a step must tag its `requires`/`provides` when it consumes or
  produces a journey capability; an untagged step is unconstrained (the pre-existing
  behavior). No migration; prompt-model and test only.
