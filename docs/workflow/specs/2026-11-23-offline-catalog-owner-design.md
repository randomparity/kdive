# Offline catalog assembly owner

## Problem

CLI and reference generators duplicate offline application construction; doc constants
reach through the reference renderer for that same catalog.

## Scope

One Cast unit implements [ADR-0836](../../adr/0836-offline-generators-share-the-catalog-assembly.md).
Current owners are both `_registry_tools` functions. Intended owner is
`scripts.generate.offline_catalog.registry_tools`. Migrate CLI, reference and constant
generators plus affected direct tests; remove both old definitions and the cross-renderer
private import without aliases. Preserve existing app assembly and renderer transforms.
No public tool/schema, runtime service, dependency or persisted-data changes.

### Failure model

- Actors/deployments: developers and CI using repository `just` generation/check recipes,
  direct scripts with the recipe's PYTHONPATH, or module entry points from repository root.
- Assets/invariants: identical tool metadata, recovery-tool inclusion, generated artifacts,
  and extraction without opening database, object store or network services.
- Accepted: extraction still requires the normal developer Python environment; this change
  does not make scripts installable outside the checkout or callable within an active loop.
- Covered elsewhere: production assembly and authorization remain owned by existing runtime
  tests; renderer-specific validation and atomic writes stay in their existing consumers.

## Success

All three consumers use one construction recipe; no renderer imports another renderer.
Fresh CLI descriptors, reference pages and doc constant values match baseline bytes.
Conditional recovery tools remain present when the runtime verifier is disabled.
Configured direct-script and module invocations continue to work without live backends.

## Validation

- focused-test: new `tests/scripts/test_offline_catalog.py` imports the new owner and builds
  the real catalog while database-open/network-connect boundaries raise; missing owner is
  the expected initial RED. Retain recovery-tool inclusion and existing assembly checks.
- focused-test: migrate existing generator tests; run `just test-verbose` on that new file
  and `tests/scripts/test_gen_{cli_verbs,tool_reference,doc_constants}.py` (explicit paths).
  Controlled missing catalog witness and attempted external connection must be caught.
- focused-test: capture complete tool metadata and generated output before changes; compare
  after. Run `just cli-verbs`, `just docs`, `just doc-constants`, then their check recipes
  and `just resources-docs-check`; expect no artifact diff. Exercise recipe-supported
  script paths and module checks. Run lint, whole-tree types and relevant doc/hook gates.
- task-test-not-applicable: moving the unchanged recipe has no new behavior beyond its
  ownership boundary; use exact AST comparison rather than inventing a behavior change.
