# External-boot refusal diagnostics (#2794)

## Problem

The external-boot runner turns a commit refusal into a categorized authority failure, but its
failure context retains only the phase. This protects the authority audit and job record from
provider text while leaving an operator unable to distinguish refusal causes in the worker log.

## Scope

`_bound_failure` owns the conversion shared by commit and provider-call failures. It emits one
warning with the job ID, activation ID, phase, exception type, and message. A fresh `Redactor`
using the operation's `SecretRegistry` processes the exception message before it reaches the
logger. The redacted message is limited to 8192 characters to bound one record's volume. The
worker's JSON log formatters escape control characters into one output line. The warning does not
attach `exc_info` or the exception object, so its traceback cannot expose unredacted provider
fields. The existing failure result and `from None` behavior remain.

This extends the runner's existing responsibility. Its callers and the persisted authority schema
need no transition.

## Failure model

- Actors and deployments: the KDIVE worker handles core and provider refusals for an operator
  running an external-boot job; the operator reads the worker log.
- Invariants and assets: registered secrets stay out of the emitted warning; provider text stays
  out of the authority result and job failure context; the existing failure category and phase
  retain their meaning.
- Accepted failure classes: provider-supplied host paths may appear in the operator worker log
  after secret redaction, because the requested diagnostic includes the exception message.
  A reason longer than 8192 characters is truncated in the log; the remaining text is available
  only from the provider. A logging handler failure follows Python logging's existing behavior.
- Covered elsewhere: the existing `SecretRegistry` and `Redactor` own known-secret redaction;
  the existing authority result model and commit validation own the persisted failure shape.

## Threat model

- Added boundary: provider or core exception message enters the operator worker log. The provider
  controls its own exception message; a tenant may indirectly influence a provider failure.
- Widened boundaries: none. The authority result and job failure context remain phase-only for
  ordinary refusals.
- Actors: an authenticated tenant may trigger a job; the provider supplies failure text; the
  worker log is trusted operator output.
- Control: construct a fresh `Redactor` from the operation's `SecretRegistry` and redact the
  message before limiting it to 8192 characters and passing it to the logger. The stdlib JSON
  formatter and OTel stdout JSON exporter serialize control characters inside the message string.
  Pass no exception object or traceback to logging. Keep the existing `from None` and failure-result
  construction unchanged.
- Out of scope: unregistered provider identifiers in the operator log are part of the requested
  diagnostic; access to operator logs is controlled by the deployment.

## Success

An operator can locate the reason for a refused commit by job and activation ID in the worker log.
The warning carries no registered secret. The authority result and job failure context retain
their existing fields, and the exception remains unchained.

## Validation

- `focused-test`: Drive a categorized commit refusal through the existing runner test vehicle;
  assert one warning includes the identifiers, phase, type, and redacted reason.
- `focused-test`: Assert the returned failure context's non-null fields contain only the phase
  and the refused message is absent from the serialized authority result.
- `focused-test`: Drive a provider exception carrying a registered secret; assert the warning
  masks that value, bounds a long reason, and the result remains unchained.
- `task-test-not-applicable`: No schema, MCP response, or provider port changes are proposed;
  their existing contract tests remain the integration guard.
