# 0684 — Independent coverage obligations

## Status

Accepted

## Context

[#2804](https://github.com/randomparity/kdive/issues/2804) requires coverage obligations that
survive missing collection and missing results. The existing census registers one configuration;
its result renderer indexes only tool and provider. The
[current architecture](../design/top-level-design.md#mcp-tool-surface) retains conditional
registrations, multiple provider capabilities and distinct native/emulated paths.

## Decision

Keep an explicit versioned operation-to-assertion mapping, expanded against the registry,
catalog and architecture/provider owners. Qualification compares results with that independent
set and separately supplied candidate/input bindings; missing or inadmissible evidence is red.
Pending mappings carry owners and assertions without claiming an executable or a live result.
The [design](../workflow/specs/2026-09-26-coverage-obligations-design.md) defines the
evidence boundary; the [contributor guide](../development/coverage-qualification.md) describes usage.

## Consequences

New tools require a reviewed mapping. Changing the matrix or inputs invalidates old evidence.
Functional success, expected rejection and reviewed unsupported decisions stay distinct.
The fast ownership check can pass before live qualification does. Later epic entries implement
the scenarios and bind fixtures; publication enforcement remains owned by #2819.
The offline verifier trusts reviewed scenario producers; it does not attest remote execution.

## Considered & rejected

- **Use pytest collection as the expected set.** verified: #2804 requires a missing or skipped
  required scenario to make qualification red, independently of what collected.
- **Keep one verdict per tool/provider.** verified: `scripts/coverage_campaign/results.py` at
  `ebd339c95` keys `CellResult` by that pair and overwrites earlier duplicates; it cannot represent
  separate functional/rejection assertions and scenario/input identities.
- **Create a separate qualification service.** judgment: the existing offline scripts and pytest
  provide the required boundary without another deployed component or production schema.
