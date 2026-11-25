# Authority job model ownership

## Problem

The authority schemas occupy most of the generic job registry module. Data
consumers import registration and its other result dependencies along with
models. Pending typed producer construction needs one direct schema owner.

## Scope and design

Follow ADR-0833. Relocate the33 existing top-level data definitions, including
private payload/evidence variants and helpers, through
ExternalBootAuthorityHandlerResult to jobs/external_boot_models.py. Preserve their
source and order. Import only their existing actual dependencies; the isolated
probe shows generic registry, psycopg and generic Job/JobKind are not needed there.

Keep JobHandlerResult, JobHandler, DuplicateHandler and HandlerRegistry at
jobs/models.py with their existing semantics. Refer to the authority union via
one private module alias; do not re-export authority names at this owner.
Move the28 actual data import sites to the model owner, preserving aliases and
splitting mixed generic/data imports. The61-file inventory distinguishes33
generic-only consumers that require no edit. No qualified or string-based model
module references were found outside these import nodes in src/tests/scripts/deploy.

No constructor, model name, field, alias, validator, schema version or worker
control flow changes here. Current public client guidance exposes no jobs.models
Python import contract; this is the internally authorized import-path migration.
Published MCP/CLI and serialized job/database contracts stay unchanged. Existing
release/teardown completion classes must remain the exact classes shared by
handler, queue and worker in the new graph, not duplicate definitions.

Global constraints: Python 3.14 managed by uv; x86_64 and ppc64le targets;
public tool contracts preserved; local changes and commits only.

## Failure model

- Actors and deployments: the four Python runtime roles, authority job producers
  and validators in local, Compose and Kubernetes deployments; developer scripts
  and tests that import the internal model owner.
- Invariants and assets: closed JSON validation and aliases; current authority
  bindings and worker result subtype; no circular import or second model identity;
  generic registry behavior and inclusion of the new module in release artifacts.
- Accepted failure classes: unsupported project-internal old import paths move
  together; a process already running old code is restarted under existing deploy
  policy, not hot-reloaded into a mixture of Python class identities.
- Covered elsewhere: unchanged SQL holder/receipt/credit fences, MCP authorization
  and provider mutation protocols; the next queued unit owns typed construction.

## Threat model

- Boundaries added/widened: none; model location changes, not accepted JSON or
  executable permissions. Queue/database/authority JSON remains the trust boundary.
- Actors/trust: authenticated callers and authority peers retain existing validation;
  assembly controls Python imports and callbacks. No untrusted import path is added.
- Controls: retain each33 definition unchanged and the closed carrier validators;
  preserve failure/success/conflict and derived worker dispatch types.
- Out of scope: changing protocol validation, authorization, persistence or host
  installation; the existing owners keep those controls. No bypass constructors.

## Success and validation

1. Data import loads its schema owner without the generic registry, and direct
   consumers use that owner. Generic-only imports and registry behavior remain.
2. All33 data-definition ASTs and the generic registry/callable contracts are
   preserved, with one qualified reference replacing the authority union name.
3. The captured25 data-model schemas and generic result schema preserve their
   validation graph; fixed alias JSON round-trips and worker receipt/replay tests
   preserve accepted/rejected variants and concrete completion subtype.
4. A built wheel includes both owners and imports them with the expected boundary.

The plan maps the existing model, payload, worker, authority handler and connected
receipt suites. Add one isolated-process import-boundary test, observe the missing
new owner before implementation, then prove it catches an injected registry edge.
Use schema snapshots and AST comparison as relocation proof, not fabricated
behavioral reds. Run focused real-role suites, lint, whole-tree types and doc/record
hooks. Build after the source commit and inspect/import the wheel before resolving.
