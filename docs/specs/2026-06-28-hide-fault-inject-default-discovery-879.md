# Hide the fault-inject fixture from default agent discovery (#879)

- Issue: #879
- ADR: [0269](../adr/0269-hide-fault-inject-from-default-discovery.md)
- Source: `BLACK_BOX_REVIEW.md` P2 — "Advertised `fault-inject` provider isn't schedulable here."

## Problem

`fault-inject` is the ADR-0072 test/mock provider (deterministic crash replay), not a production
provisioning lane. `systems.profile_examples` (ADR-0124) — the read-only, auth-only tool that lets
a cold agent learn a valid profile shape from the MCP surface — advertises `fault-inject` as a
first-class provider whenever the inventory configures no provider instance. A black-box agent read
the example and attempted to allocate it; the allocation only failed because no `fault-inject`
resource was registered. The capability is described, then absent — a wasted agent turn.

## Goal

`fault-inject` is not presented as a provider option in `profile_examples` unless the environment
actually configures a `[[fault_inject]]` instance; when it is presented, it is clearly marked
test-only; a guard test prevents it reappearing in the default set.

## Non-goals

- The `fixtures` MCP namespace (`fixtures.list`/`validate`) — that is the rootfs baseline catalog
  (ADR-0089 §6), whose default profiles are all `local-libvirt`; it does not surface the fault-inject
  provider and is left unchanged.
- The `platform_admin`-only `resources.register_fault_inject` tool and the `_KIND_BY_BLOCK` map —
  operator surface, not general agent discovery; unchanged.
- Removing `fault-inject` from the `ResourceKind` enum or `allocations.request` schema — the provider
  is real and registerable in test/dev; only its *default discovery* advertising is removed.

## Current behavior (`src/kdive/mcp/tools/lifecycle/systems/profile_examples.py`)

`_configured_providers(doc)` returns the providers to emit an example for:

- `doc is None` → `[_LOCAL, _REMOTE, _FAULT]` (default placeholder set).
- otherwise, append each configured kind; `or [_LOCAL, _REMOTE, _FAULT]` when none configured.

So a server with no `[[fault_inject]]` block still advertises the fixture. The `doc.fault_inject`
branch (when an instance *is* configured) is correct and stays.

## Change

1. `_configured_providers`: the default and fallback lists become `[_LOCAL, _REMOTE]`. The
   `if doc.fault_inject: configured.append(_FAULT)` branch is unchanged — a configured fault-inject
   instance still yields an example.

2. `_example_item`: add `"test_only": provider == _FAULT` to the item `data` (uniform key on every
   item; `True` only for fault-inject).

3. `src/kdive/domain/catalog/resources.py`: comment `ResourceKind.FAULT_INJECT` as the ADR-0072
   test fixture kind, distinct from the production provider kinds.

## Acceptance criteria → verification

- **Not advertised by default** — `build_profile_examples(None)` and a doc configuring no
  `fault_inject` emit items for `local-libvirt`/`remote-libvirt` only; no `fault-inject` item. New
  guard test asserts this.
- **Marked test-only when present** — a doc with a `[[fault_inject]]` instance emits a `fault-inject`
  item whose `data["test_only"]` is `True`; production items carry `False`. New test asserts this.
- **Guard against regression** — the default-set test fails if `fault-inject` is re-added to the
  default/fallback lists.

## Edge cases

- Empty doc (`InventoryDoc.parse({...no instances})`) → `[local, remote]` (the `or` fallback path).
- Doc configuring only `[[fault_inject]]` (no local/remote) → exactly one `fault-inject` item,
  `test_only=True`. (The fallback only fires when the configured list is empty; a configured
  fault-inject makes it non-empty.)
- `None` doc → `[local, remote]`; both placeholder examples still parse + pass policy.

## Rollback

Pure revert: restore `_FAULT` in the two default lists and drop the `test_only` key and the guard
test. No migration or persisted state involved.
