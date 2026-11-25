# 0836 — Offline generators share the catalog assembly

## Status

Accepted (2026-11-23)

## Context

The CLI verb and tool reference generators repeat the same offline application
assembly. Doc constants import the reference renderer's private builder.
The three consumers need the same tool catalog, including conditional recovery tools.

## Decision

Move the recipe to `scripts/generate/offline_catalog.py` as `registry_tools`.
All three generators call it directly. Retain the unopened pool, generated verifier,
inert object store, provider composition and catalog lifecycle witness.
Renderers keep their own transformations; production assembly remains unchanged.
Run the CLI and reference recipes as modules, matching the constants recipe, so
the repository root resolves the shared generator package without path bootstrapping.

## Consequences

Catalog construction has one generator-side owner. Private renderer helpers disappear
without compatibility aliases. The `docs`, `cli-verbs` and `cli-verbs-check` commands
retain their names and generated output; their internal Python calls become module
invocations. Extraction still needs the normal developer environment.

## Considered & rejected

- **Keep two copies.** judgment: changes to the offline recipe would still need two edits.
- **Import the reference renderer from other generators.** judgment: catalog construction
  does not belong to one output format.
- **Reuse the test registry fixture.** judgment: generation should not import test support.
- **Keep direct-file recipe calls with import fallbacks.** judgment: the existing module
  entry convention avoids dual import paths or custom sys.path manipulation.
