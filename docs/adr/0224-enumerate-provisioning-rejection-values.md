# ADR 0224 — Enumerate valid catalog names and roots in provisioning rejections

- **Status:** Accepted
- **Date:** 2026-06-23
- **Deciders:** kdive maintainers

## Context

Two provisioning rejections name the bad value but discard the valid set the server already
holds, so a pure-MCP agent cannot self-correct a typo'd reference without host access (#731,
part of #736; same root cause as the closed #449 finding 2). `validate_rootfs_reference`
(`profiles/provisioning.py:401-423`) raises `unknown rootfs catalog name: …` after iterating the
declared `[[image]]` inventory in `_catalog_name_declared`; `validate_local_component_path`
(`components/local_paths.py:13-34`) raises `… is outside provider allowed roots` while holding
the `allowed_roots` parameter. Both discard the valid set.

The canonical attach-point, `config_error_reason(..., accepted_values=…)` (ADR-0174), builds a
`ToolResponse`, but these sites raise `CategorizedError` deep in a connectionless validator. The
error reaches the wire through `safe_error_details` (`serialization.py:91-108`), shared by the
admission path (`services/systems/admission.py:166-173` → provision response) and
`ToolResponse.failure_from_error` (`mcp/responses.py:197`). That filter reduces every detail
value to a finite JSON scalar and **drops every non-scalar except a reserved `errors` list**. A
`details["accepted_values"]` list would be silently dropped today — and two existing sites
(`profile_policy.py:31-35`, `details={"unsupported": [...], "supported": [...]}`) already lose
their lists to this filter unnoticed, with no test catching it.

## Decision

We will (1) extend `safe_error_details` to preserve a **bounded list of JSON scalars** under a
small set of reserved enumeration keys (`accepted_values`, `available`), mirroring the existing
`errors`-list reservation — element count capped at the existing `_MAX_ERROR_ENTRIES` bound,
non-scalar elements dropped; and (2) populate `details["available"]` (declared `provider/name`
catalog entries) at the unknown-catalog-name site and `details["accepted_values"]` (configured
roots) at the outside-allowed-roots site, both sorted for a stable wire order. Any list under a
non-reserved key is still dropped — behaviour for every other detail key is unchanged.

## Consequences

- A black-box MCP agent can recover from a typo'd `catalog` name or an out-of-roots `local`
  path from the rejection envelope alone, satisfying #731's acceptance criteria through the
  existing `data` channel with no new tool surface, port, schema, migration, or dependency.
- `safe_error_details` gains one reserved-key branch. The no-leak invariant (ADR-0123) holds
  because the only values placed under the reserved keys are operator-declared catalog names
  and operator-configured roots — never caller input, secrets, hostnames, or object-store keys.
  The per-element scalar filter and the `_MAX_ERROR_ENTRIES` cap bound envelope size.
- The change makes preserving `profile_policy.py`'s `unsupported`/`supported` lists *possible*
  but does not wire them — left to a follow-up to keep this diff scoped to #731.

## Alternatives considered

- **Join the list into a scalar string in `details`** (e.g. `available: "a/b, c/d"`). Keeps
  `safe_error_details` untouched but forces the agent to parse prose back into a set, defeating
  the machine-readable intent of ADR-0174's `accepted_values` (a list). Rejected.
- **Build the `ToolResponse` with `config_error_reason` at the rejection site.** The site is a
  connectionless validator that raises `CategorizedError` by contract; it has no `object_id` and
  no response-shaping responsibility. Threading response construction down would invert the
  layering. Rejected.
- **Special-case only `accepted_values`.** The unknown-catalog case is naturally an `available`
  set (what exists), not an `accepted_values` set (what this field admits). Reserving both keys
  matches existing vocabulary (`available_kinds`, ADR-0174 `accepted_values`) and costs nothing
  extra. Chosen over a single key.
- **Lift the cap for enumerations.** An unbounded list lets a large inventory inflate an error
  envelope. Reusing the existing `_MAX_ERROR_ENTRIES` bound keeps envelopes bounded. Rejected
  unbounded.
