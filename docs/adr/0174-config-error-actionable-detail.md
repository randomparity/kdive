# ADR 0174 — Actionable detail on configuration-error envelopes

- **Status:** Accepted
- **Date:** 2026-06-18
- **Deciders:** kdive maintainers

## Context

ADR-0123 added a `detail` field to `ToolResponse` and ADR-0166 enriched the
`bad_artifact_declaration` rejection with a `field`, an offending value, and the accepted
vocabulary. Those changes covered the upload lane. The remaining parse/validation
`configuration_error` sites still return a bare envelope: `error_category` is set, but
`detail` is `null` and `data` is empty.

The shared helper permits this — `config_error(object_id)` defaults both `detail` and `data`
to empty (`src/kdive/mcp/tools/_common.py:28-33`). The bare sites a black-box MCP client hits:

- Allocation read: malformed `allocation_id`, non-finite `timeout_s`
  (`lifecycle/allocations/view.py:35-69`).
- System / run read: malformed UUID, unknown `state` filter, malformed PCIe spec
  (`lifecycle/systems/view.py:68-75,121-149`, `lifecycle/runs/view.py:22-33`).
- Investigation surface: title/description bounds, malformed external ref, malformed UUID,
  empty `set` payload, malformed ref key, unknown `state` filter
  (`catalog/investigations.py:190-204,420-443,505-513,559-572`).
- Debug session: malformed UUID, unknown transport, detached session with no System
  (`debug/sessions_lifecycle.py:259-280,347-372`).

A client that receives only `configuration_error` cannot tell which field was wrong, what the
accepted values are, or what to do next — it must guess. The acceptance criteria for #569
require every returned `configuration_error` to carry at least one of `detail` or
`data.reason`, with field-specific reasons and, where the valid set is finite, the accepted
values.

`CONFIGURATION_ERROR` is **not** a suppressed category (`_SUPPRESSED_DETAIL` in
`src/kdive/domain/errors.py` holds only `authorization_denied` and `not_found`), so `detail`
and `data` pass through unchanged for these sites — there is no no-leak conflict, because a
malformed id the caller supplied carries no cross-project existence signal.

## Decision

Thread a machine-readable `reason` (and, where applicable, `accepted_values`) plus a
fixed-template human `detail` through every parse/validation `configuration_error` site named
above, following ADR-0166's `reason`/`field`/`accepted_*` shape.

### 1. A small reason vocabulary

A closed set of reason tokens in `src/kdive/mcp/tools/_common.py`, surfaced under
`data.reason`:

- `invalid_uuid` — a syntactically malformed object id.
- `invalid_state` — an unknown lifecycle-state filter value.
- `invalid_transport` — an unknown debug transport.
- `invalid_external_ref` — a malformed investigation external reference (or ref key).
- `missing_required_field` — a required edit field absent (e.g. `set` with neither title nor
  description).
- `invalid_timeout` — a non-finite wait timeout.
- `invalid_text` — a title/description outside its length bounds.
- `invalid_pcie_match` — a PCIe match spec missing vendor/device id.

### 2. Helper changes

`config_error` keeps its signature. A new `config_error_reason` helper builds the envelope
from a reason token plus an optional `accepted_values` list and an optional fixed-template
`detail`, so a call site states the field-specific reason in one place:

```python
config_error_reason(object_id, reason="invalid_uuid", detail="…")
config_error_reason(object_id, reason="invalid_state",
                    accepted_values=[s.value for s in SystemState], detail="…")
```

The reason and accepted values land in `data` (machine-actionable, never suppressed for
`configuration_error`); the human string lands in `detail`. `accepted_values` is sorted for a
stable wire order.

### 3. `detail` text is author-controlled

Every `detail` string is a fixed template that interpolates only the reason/field name and,
for the malformed-id case, the offending id the caller already supplied. No `detail`
interpolates a secret, secret-ref path, internal hostname, object-store key, or a resource
name the caller did not provide — consistent with ADR-0123's egress rule. The malformed PCIe
site already routes its parse error through `ToolResponse.failure_from_error`
(`failure_from_error` surfaces the `CategorizedError` message); that path is unchanged and the
bare `vendor/device` fallback gains the `invalid_pcie_match` reason.

## Consequences

- A black-box MCP client gets a self-correcting envelope for every parse/validation failure:
  `data.reason` names the failure class, `accepted_values` enumerates the finite valid set, and
  `detail` is a human one-liner.
- The reason vocabulary is closed and tested: a drift guard asserts every reason a call site
  emits is in the vocabulary set, so a typo cannot ship.
- No envelope shape change: `reason`/`accepted_values` are ordinary `data` keys and `detail` is
  the existing field. No migration, no schema change.
- The no-leak not-found seam is untouched: the `not_found` sites (a valid id with no visible
  row, including an id in an ungranted project) still return the byte-identical suppressed
  envelope. Only the *malformed-id* / *invalid-argument* branches gain detail.

## Considered & rejected

- **Auto-deriving `detail` from `str(exc)` everywhere.** Most of these sites do not raise a
  `CategorizedError`; they return early from a plain validity check. Routing them through an
  exception only to extract a message adds indirection for no gain, and a free-form message is
  the egress risk ADR-0123 warned about. Fixed templates are clearer and safer.
- **Adding a new `ErrorCategory`.** The taxonomy is intentionally closed and these are all
  genuine `configuration_error`s. A new category would fragment the existing handling and the
  retryable mapping. The `reason` token is the right granularity.
- **Surfacing the offending value for every field.** ADR-0166 already showed the value is only
  safe to echo for a short caller-supplied string (the malformed id). Echoing arbitrary field
  values risks size blowups and reflected untrusted content, so only the malformed id (already
  the `object_id`) and the closed accepted-value sets are surfaced.
