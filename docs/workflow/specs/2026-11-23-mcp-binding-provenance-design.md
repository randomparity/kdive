# Share precise MCP caller-validation provenance

## Problem and owner

Actual direct and gateway dispatch through FastMCP plus binding/server-fault middleware both
convert an unrelated body model's `profile` error to the curated provisioning caller envelope.
The private reproduction has two RED body cases and two genuine-binding controls passing in1.10s.
The middleware accepts bare Pydantic errors; the gateway independently requires FastMCP wrapping.
[ADR-0844](../../adr/0844-mcp-validation-provenance-belongs-to-one-classifier.md) records one owner.

## Shared classification

Add `binding_validation_error(exc: Exception) -> ValidationError | None` to
`mcp/middleware/binding_errors.py`. Return `exc.__cause__` only if the outer exception is
FastMCPValidationError and the immediate cause is a Pydantic ValidationError. Every other shape
returns None: bare Pydantic, non-Pydantic cause, missing cause, deeper chain or other outer type.
Do not infer provenance from an error location or recursively unwrap arbitrary exceptions.

Middleware catches FastMCPValidationError only, calls the classifier, and re-raises when it
returns None. Feed a recognized cause to the existing curated matching/building path; unmatched
wrapped errors re-raise unchanged. Delete the bare-Pydantic conversion path. Keep conversion
entries, builders, object-ID lookup, error sanitation, ToolResult and middleware order unchanged.

Gateway calls the same helper in its existing validation-error catch. Replace the manual
cause/type interpretation with that result and `cause is not None`; retain all response building,
field-error limits, context/visibility handling, schema disclosure and suggested-action logic.
Bare body errors retain the existing generic tools.invoke configuration_error envelope with no
field detail or discovery actions. No public schema, response-class or error taxonomy change.

## Explicit caller reconstruction

The one verified in-body caller site is the flat `allocations.request` wrapper rebuilding
AllocationRequestPayload. Enclose only that construction in try/except Pydantic ValidationError;
raise FastMCPValidationError from the original, with log_level=logging.WARNING, matching installed
FastMCP's own binding path. Preserve argument construction, all subsequent guards/handler calls,
exception message, public signature and docstring. This explicit caller annotation keeps the
existing shape-XOR curated envelope without teaching middleware that all body validation is input.
Other model/handler failures remain outside the catch. No generic wrapping decorator is needed.

## Existing tests and provenance

Existing unit fixtures label bare Pydantic errors as genuine binding. Update only the positive
binding cases to construct the actual FastMCP wrapper/cause, retaining raw negatives and precise
propagation assertions. Do not blanket-wrap every test exception. Keep existing envelope IDs,
profile/build/sizing detail, sanitized errors and direct/gateway binding assertions intact.

The server-fault test's fake systems.provision body validates a constant malformed row, although
its comment calls it caller input. Correct that premise: it is a bare body error and must no
longer receive curated attribution. Test actual caller-input reconstruction through the real
allocation registrar, independently of the corrected fixture. Keep ADR0709's stored/response
fault arms and logging assertions unchanged. Preserve gateway hidden-tool nondisclosure tests.

## Failure model

- Installed FastMCP's outer-wrapper/immediate-cause distinction is the classifier contract.
  Upgrades changing it must fail the real dispatch tests; no version/floor change is introduced.
- Field names are not authority. Bare stored/body errors propagate through middleware even when
  every location matches a curated profile/build/sizing conversion.
- The explicit allocation catch covers only values just supplied by the caller, before any
  handler/database lookup. Unrelated code failures and stored-data validation are not retagged.
- The gateway's existing generic bare-body configuration_error envelope and underlying FastMCP
  compatibility warning remain accepted protected behavior; this unit adds no broad fault mapping.
- Curated envelope sanitation and gateway visibility/disclosure are unchanged. No auth/usage/audit
  gate or middleware ordering moves, and no cause-chain search can mistake a deeper body error.
- Direct raw exceptions and gateway generic envelopes intentionally remain distinct existing
  transport outcomes. The shared rule only decides whether a failure has caller-binding provenance.

## Validation

Promote the unchanged real four-case reproduction to permanent direct/gateway dispatch tests.
Require body cases RED before implementation and genuine-binding controls to remain GREEN.
Exercise missing/wrong/deeper causes and unrelated outer errors, plus recognized errors under
unmatched tools/locations; assert exception identity survives propagation.

Exercise the actual allocation wrapper through direct and gateway calls for both/neither sizing
and valid construction reaching its handler. Keep profile/build envelope shapes/IDs/sanitization
and hidden-tool schema nondisclosure. Run focused middleware, core binding, server-fault, gateway,
allocation and metadata/schema tests with Docker required where applicable.

Fault acceptance includes allowing bare errors, searching deeper causes, dropping explicit caller
wrapping and changing the visibility gate. Require named test failures and restore exact bytes.
Compare original conversion tables/builders/ID extraction and gateway construction/visibility AST,
allocation construction/handler body, middleware order and all136tool metadata/CLI/reference bytes.
Run lint/whole-tree types, relevant documentation checks, record checks and hooks, actual
wheel import/source proof and fresh scoped code/security reviews. Final campaign CI/reviews remain required beyond this Cast unit.
