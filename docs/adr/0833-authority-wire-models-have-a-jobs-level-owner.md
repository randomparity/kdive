# 0833 — Authority wire models have a jobs-level owner

## Status

Accepted (2026-11-23)

## Context

At a9a79b840, jobs/models.py combines authority marker, evidence, result and
completion schemas with generic handler types and registration. An AST inventory
finds33 authority data definitions and28 affected import sites among61 files
importing the module. Typed producer construction is the next queued change.

An earlier standalone move was declined because the generic handler union still
needs authority types. That does not require data consumers to import the generic
registry. An isolated Python3.14 probe loads the unchanged data definitions with
their actual dependencies and no generic registry;25 model JSON schemas remain
equal under the proposed owner name. No whole-registry startup speedup is claimed.

## Decision

Move the existing authority definitions to jobs/external_boot_models.py and
point data consumers directly at that owner. Keep JobHandlerResult, JobHandler,
DuplicateHandler and HandlerRegistry in jobs/models.py. The generic result union
uses qualified references to the data owner. Do not leave compatibility re-exports
or forwarding modules for these project-internal import paths.

Preserve definition names, model fields, validators, discriminators, aliases,
defaults and concrete completion classes in this transition. Protected MCP/CLI
and serialized job/database contracts do not change. Typed producer construction
follows separately against the model owner; this move adds no new validation rule.

## Consequences

Schema-only consumers no longer acquire generic registration as an import
side effect. The generic registry still depends on authority data through its
result union. Internal callers migrate together; workers continue to exchange
JSON and retain exact completion subtype checks. Package discovery must include
the new module, proven with a built wheel. No schema migration is required.

## Considered & rejected

- **Keep both responsibilities together.** judgment: the pending constructor
  work exposes an existing mixed owner; direct model consumers need no registry.
- **Re-export authority names at the old module.** judgment: a second import
  path preserves the dependency the move is meant to remove, with no supported
  client import contract requiring it.
- **Remove the authority type from the generic result union.** verified:
  JobHandlerResult at a9a79b840 explicitly admits authority completions, and
  worker dispatch tests depend on their concrete success/failure/conflict types.
