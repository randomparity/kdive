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
Switch only `docs`, `cli-verbs` and `cli-verbs-check` Python calls in `justfile` to
module entry points, retaining command names and the working-tree import-path pin.
No public tool/schema, runtime service, dependency or persisted-data changes.

### Failure model

- Actors/deployments: developers and CI using repository `just` generation/check recipes,
  using module entry points from repository root. Bare direct-file invocation is internal
  and replaced; no import fallback or custom path bootstrap is added.
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
The configured `just` commands and module invocations work without live backends.

## Validation

- focused-test: new `tests/scripts/test_offline_catalog.py` imports the new owner and builds
  the real catalog while database-open/network-connect boundaries raise; missing owner is
  the expected initial RED. Retain recovery-tool inclusion and existing assembly checks.
- focused-test: migrate existing generator tests; run `just test-verbose` on that new file
  and `tests/scripts/test_gen_{cli_verbs,tool_reference,doc_constants}.py` (explicit paths),
  plus the direct catalog fixture in `tests/cli/test_verb_schema_guard.py`.
  Controlled missing catalog witness and attempted external connection must be caught.
- focused-test: capture complete tool metadata and generated output before changes; compare
  after. Run `just cli-verbs`, `just docs`, `just doc-constants`, then their check recipes
  and `just resources-docs-check`; expect no artifact diff. Exercise recipe-supported
  module checks with recipe PYTHONPATH and ambient shadow coverage in
  `tests/scripts/test_generated_doc_gates_working_tree.py`. Run lint, whole-tree types
  and relevant doc/hook gates.
- task-test-not-applicable: moving the unchanged recipe has no new behavior beyond its
  ownership boundary; use exact AST comparison rather than inventing a behavior change.
