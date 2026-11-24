# Keep CLI command selection separate from execution

## Problem

`dispatch._dispatch` calls `registry.run_verb`, which imports dispatch again to
invoke the generic handler. The registry mixes descriptor selection with async
execution and creates a reverse dependency despite the existing execution owner.

## Scope

Following [ADR-0866](../../adr/0866-cli-execution-ownership.md), replace the internal registry execution entrypoint with
`select_verb(args) -> tuple[GeneratedVerb, Handler | None]`. It performs the same
path lookup and returns the same optional override without calling it. Dispatch
selects once, then awaits the override or its existing generic handler.

Remove `run_verb` without an alias; migrate its direct test callers to the real
dispatch entrypoint or selector. Keep parser/descriptor generation, handler map,
bespoke routes, unknown-command error text, payload assembly, live tier/token/
confirmation ceremony, rendering and exit mapping. Other findings retain their
existing queue owners; this introduces no new runtime module or public surface.
Update the existing lifecycle proof docstrings that name the removed executor.

### Failure model

- Actors/deployments: supported CLI users; local Linux x86_64 verification of
  architecture-neutral Python behavior, with x86_64/ppc64le targets retained.
- Assets/invariants: parsed arguments and selected handler/descriptor identity;
  unchanged MCP payload and security ceremony, unknown-path failure semantics.
- Accepted classes: external transport/IdP/server failures retain their current
  handling; no new live deployment or hardware qualification is promised.
- Covered elsewhere: existing CLI transport/privacy, live annotation tier,
  token-expiry, confirmation and server authorization tests retain their roles.

## Success

The registry selects synchronously without importing or calling dispatch.
Descriptor overrides and generic commands execute through dispatch; unknown
paths keep their exact errors. Existing public command behavior remains intact.

## Validation

- `focused-test`: a structure-aware import-boundary guard in existing
  `tests/cli/test_dispatch_wiring.py` fails on the current deferred import and
  on a controlled reintroduced dependency. Selector cases verify no execution,
  descriptor/override identity, default None and exact unknown-path errors.
- `focused-test`: existing real-parser/dispatch tests and schema payload capture
  migrate to the actual execution owner. Full CLI tests cover payload/tier/token/
  confirmation/render/exit behavior at transport boundaries.
- Command: `uv run python -m pytest tests/cli -q --tb=short`.
  Run `just cli-verbs-check`, `just lint`, whole-tree `just type`, hooks and an
  independent code review; restore controlled faults before the final green run.
