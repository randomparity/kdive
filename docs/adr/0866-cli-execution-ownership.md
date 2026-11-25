# 0866 — Keep CLI registry selection below dispatch execution

## Status

Accepted (2026-11-24)

Builds on [ADR-0423](0423-generic-generated-verb-dispatch.md); its MCP payload and
live-tier ceremony remain in the existing generic handler.

## Context

The dispatcher calls the registry's async executor, which imports the dispatcher
again for generic commands. Path selection and execution therefore point both ways.
The registry already owns descriptors, parser construction and handler lookup.

## Decision

The registry returns the selected descriptor and optional handler synchronously.
The dispatcher awaits that override or its existing generic handler. Remove the
old internal execution entrypoint and migrate direct callers without an alias.
Keep command grammar, handler map, errors, payloads and security ceremony unchanged.

## Consequences

Registry selection no longer depends on dispatch. Execution stays with its
existing owner, without another module, injected callback or public CLI change.
Internal test callers use the actual execution entrypoint when proving payloads.

## Considered & rejected

- **Keep the deferred reverse import.** verified: AST inspection of
  `src/kdive/cli/commands/registry.py` at `376a1b4ffeb0` finds the function-local
  `from kdive.cli import dispatch` in `run_verb`; deferral preserves that edge.
- **Inject the generic executor into the registry.** judgment: callback plumbing
  retains execution in the selection owner when dispatch can execute directly.
- **Add a third execution module.** judgment: moving the generic handler and its
  ceremony creates an unnecessary owner; the existing dispatcher already suffices.
