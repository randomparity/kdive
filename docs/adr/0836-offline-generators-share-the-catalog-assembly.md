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

## Consequences

Catalog construction has one generator-side owner. Private renderer helpers disappear
without compatibility aliases. These internal scripts retain their configured invocation
paths and generated output. Extraction still needs the normal developer environment.

## Considered & rejected

- **Keep two copies.** judgment: changes to the offline recipe would still need two edits.
- **Import the reference renderer from other generators.** judgment: catalog construction
  does not belong to one output format.
- **Reuse the test registry fixture.** judgment: generation should not import test support.
