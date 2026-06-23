# Spec — Enumerate valid rootfs/catalog values in provisioning rejections (#731)

- **Status:** Draft
- **Date:** 2026-06-23
- **Issue:** #731 (part of #736)
- **ADR:** [ADR-0224](../adr/0224-enumerate-provisioning-rejection-values.md)

## Problem

Two provisioning rejections name the bad value but discard the valid set the server already
holds, so a pure-MCP agent cannot self-correct a typo'd reference without shelling into the
host. This is the same root cause as the closed epic #449 finding 2: "the server holds the
information the caller needs and discards it before the wire."

1. **Unknown catalog name** — `validate_rootfs_reference`
   (`src/kdive/profiles/provisioning.py:401-423`) raises
   `CategorizedError("unknown rootfs catalog name: …", details={"provider", "name"})`. The
   declared `[[image]]` inventory it just consulted in `_catalog_name_declared`
   (`provisioning.py:426-438`) is discarded.
2. **Out-of-allowed-roots local path** — `validate_local_component_path`
   (`src/kdive/components/local_paths.py:13-34`) raises a bare
   `"local component path is outside provider allowed roots"`. `allowed_roots`
   (a function parameter) is never surfaced.

## Why the obvious fix does not work

`config_error_reason(..., accepted_values=…)` (`mcp/tools/_common.py:172-192`, ADR-0174) is the
canonical place to attach a finite valid set, but neither rejection site builds a `ToolResponse`
— they raise `CategorizedError` deep in a connectionless validator. The error rides to the wire
through `safe_error_details` (`src/kdive/serialization.py:91-108`), used by **both** consumers:

- the admission path (`services/systems/admission.py:166-173` →
  `mcp/tools/lifecycle/systems/provision.py:65-71`), and
- `ToolResponse.failure_from_error` (`mcp/responses.py:197`).

`safe_error_details` reduces every detail value to a finite JSON **scalar** and **drops every
non-scalar**, with one reserved exception: an `errors` list. A `details["accepted_values"]`
list would therefore be silently discarded today — there is no test asserting it survives, and
two existing sites (`profile_policy.py:31-35` `details={"unsupported": [...], "supported": [...]}`)
already lose their lists to this filter unnoticed.

So the fix is in two parts: (a) teach `safe_error_details` to preserve a bounded list of JSON
scalars under reserved enumeration keys, then (b) populate those keys at the two sites.

## Requirements

### Functional

- **R1.** An unknown rootfs catalog name returns `data.available` listing the declared
  `(provider, name)` catalog entries as `"provider/name"` strings, sorted, stable wire order.
  When no `systems.toml` is declared (the file is absent), the rejection cannot fire — the
  validator defers to the DB fetch — so this case has no enumeration to add.
- **R2.** An out-of-allowed-roots local-path rejection returns `data.accepted_values` (or
  `data.available`) naming the configured `allowed_roots` as absolute path strings, sorted,
  stable.
- **R3.** `safe_error_details` preserves a bounded list of JSON scalars under the reserved
  enumeration keys (`accepted_values`, `available`) — element count capped, non-scalar
  elements dropped — mirroring the existing `errors`-list reservation. Any other list-valued
  detail key is still dropped (unchanged behaviour).

### Non-functional / invariants

- **R4 (no-leak, AC#5).** Enumerated values are only operator-declared catalog names and
  provider roots. They never include secrets, internal hostnames, object-store keys, secret-ref
  paths, or any caller-submitted value (ADR-0123). Catalog names come from `systems.toml`
  `[[image]]` declarations; roots come from the operator-configured `allowed_roots`. Neither is
  caller input.
- **R5 (bounded).** The preserved list is capped at a fixed maximum element count (reuse the
  existing `_MAX_ERROR_ENTRIES = 20` bound) so a large inventory cannot inflate an error
  envelope unboundedly.
- **R6.** No change to the MCP tool surface, ports, schema, migrations, or dependencies. The
  `error_category` stays `configuration_error`; only `data` gains the enumeration.

## Failure modes / edge cases

| Case | Expected behaviour |
|------|--------------------|
| No `systems.toml` declared | Catalog rejection never fires (defer to DB). No enumeration; not regressed. |
| `systems.toml` declares zero images | Rejection fires; `available` is `[]` (empty list survives the filter). |
| Inventory > 20 images | `available` truncated to 20 entries (R5). Deterministic: sort, then truncate. |
| Empty `allowed_roots` | Path is outside any root (vacuously); rejection names `accepted_values: []`. |
| Non-scalar inventory value (defensive) | A non-string element is dropped by the per-element scalar filter, not the whole list. |
| `details` carries a list under a non-reserved key | Still dropped (R3) — no behaviour change for `unsupported`/`supported` unless explicitly migrated. |

## Out of scope

- Reworking `profile_policy.py`'s `unsupported`/`supported` details (a separate rejection, not
  named in #731). The `safe_error_details` change makes preserving them *possible*, but this
  spec only wires the two #731 sites; touching `profile_policy` is left to a follow-up to keep
  the diff scoped.
- The non-existence / unreadable / not-a-file path rejections (`local component path does not
  exist`, etc.) — those name no finite valid set.

## Acceptance tests

- `safe_error_details` preserves `available`/`accepted_values` as a bounded list of scalars,
  drops non-scalar elements, and still drops a list under a non-reserved key.
- Unknown catalog name with a declared inventory → `details["available"]` lists declared
  `provider/name` entries sorted; survives `safe_error_details`.
- Out-of-roots local path → `details["accepted_values"]` lists the configured roots sorted;
  survives `safe_error_details`.
- A live end-to-end assertion through the consumer that the enumeration reaches the response
  `data` (admission path or `failure_from_error`), proving the filter no longer drops it.
- No-leak: enumerated values contain only the declared names/roots, never caller input or a
  secret-shaped string.
