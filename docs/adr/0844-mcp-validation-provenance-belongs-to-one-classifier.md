# 0844 — MCP validation provenance belongs to one classifier

## Status

Proposed

## Context

`BindingErrorMiddleware` recognizes both bare Pydantic errors and FastMCP-wrapped errors
when their locations match a curated tool. The gateway independently recognizes only a
FastMCP validation error with an immediate Pydantic cause. A real direct/gateway proof
misclassifies two body errors under `profile` as caller errors; two genuine-binding controls pass.
Installed `FunctionTool.run` distinguishes binding from body validation by wrapping only the
former. A matching field location does not establish caller provenance.

[ADR-0132](0132-allocation-denial-ergonomics.md) preserves curated sizing errors;
[ADR-0372](0372-flat-params-for-mutation-tools.md) flattened allocation parameters, whose wrapper
rebuilds `AllocationRequestPayload` inside its body. That one known caller-input site must retain
its curated errors. [ADR-0709](0709-invalid-envelope-is-a-server-fault.md)'s explicit stored-data
and server-fault handling stays unchanged.

## Decision

Keep one `binding_validation_error(exc)` classifier in the existing binding middleware module.
Return a Pydantic cause only when the outer exception is FastMCPValidationError and its immediate
cause is a Pydantic ValidationError. Do not inspect locations, recurse causes or accept bare errors.
Both middleware and gateway call this helper before their existing response-building paths.

Remove middleware's bare-Pydantic catch. Preserve its conversion table, builders, ID extraction
and sanitization. Preserve the gateway's generic bare-body envelope, field-error bound, discovery
steer and visibility checks. This change narrows curated attribution, not all server-error policy.

At `allocations.request`'s reconstruction of the flat caller arguments, catch Pydantic validation
and explicitly raise FastMCPValidationError from it, using the framework's WARNING classification.
This is a known caller boundary, not location-based inference. Do not wrap other body errors.
Existing sizing envelopes, project IDs and all public schemas remain the same.

## Consequences

A server-side body error cannot impersonate a curated binding failure by matching field names.
Explicit caller reconstruction continues to produce the established sizing guidance. Direct bare
body errors propagate; the gateway keeps its existing generic configuration-error response without
schema fields or discovery actions. Underlying FastMCP compatibility logging can still describe a
bare error as invalid arguments. Neither generalized server-fault conversion nor that dependency's
logging behavior is changed here.

## Considered & rejected

- **Keep location-based inference.** verified: the actual four-case in-process proof has two
  body-error failures and two genuine-binding passes; locations alone give false caller attribution.
- **Delete all body caller conversions.** verified: the flattened allocation wrapper reconstructs
  `AllocationRequestPayload` at registrar.py's caller boundary, and ADR0132's sizing contract relies
  on the resulting cross-field error. Explicitly tag that verified input instead.
- **New shared exception hierarchy or normalization layer.** judgment: the installed FastMCP
  wrapper/cause contract already distinguishes provenance and the existing module owns conversion.
  A second protocol adds readers and migration without strengthening this bounded rule.
- **Convert every bare body error to infrastructure_failure.** judgment: that expands the public
  gateway response and ADR0709's allowlisted server-fault policy beyond this provenance repair.
