# Restore provider-scoped diagnostics

## Authority and outcome

Issue #3154 and accepted ADR-0091 section 2 require a named registered provider
or all registered providers, with provider-independent secret_ref coverage.
The existing ops.diagnostics Field and factory docstring already promise this.
Scope6049815461 preserves this API; user-approved exclusions are secret proof
#3155, operator mutations/recovery #3110/#3111 and native POWER #2818.
No new ADR is needed to restore this accepted decision; reserved0735 stays unused.

## Cause and proposed correction

The production factory ignores provider and passes every enabled contribution
to server checks, worker dispatch/substitution and optional egress construction.
A direct reproduction requesting provider-a returned both A and B worker checks.

Filter the supplied contributions by exact provider name before evaluating
contribution hooks. None preserves the current all-enabled behavior. Do not
normalize spelling or treat an empty string as omission. The existing enablement
predicate still gates selected contributions. A named target without an enabled
matching contribution raises CategorizedError(CONFIGURATION_ERROR) before any
check or worker dispatch is assembled; unknown and disabled names both fail
closed rather than running unrelated providers or returning core-only success.
The existing MCP assembly-failure path converts that exception into an audited
error result with no internal exception details exposed. Preserve that envelope.

Use the selected enabled contributions for every provider-owned check family;
keep secret_ref on all valid calls and preserve existing egress-unavailable
rejection. An unselected contribution's enablement or check hooks must not run.
Worker execution logic, timeouts, authorization, audit and providers themselves
remain unchanged. Clarify existing wrapper/factory documentation for enabled
selection, core checks and invalid-target error behavior.

## Verification

- Red/green multiple-provider tests exercise server, worker substitution,
  worker dispatcher construction and egress selection; None still fans out.
- Assert unknown, disabled and empty targets fail before check construction;
  unrelated throwing hooks must remain untouched. Disabled contributions remain
  omitted in all-provider runs.
- Test the real factory through the existing MCP handler error boundary and audit.
- Run focused diagnostics/MCP owning tests, lint, whole-tree type and commit hooks.
- Deploy an exact candidate to the assigned existing stack and call ops.diagnostics
  through real authenticated MCP/HTTP with omitted/local/unknown/disabled targets.
  Observe actual result providers, retained core check and invalid-target error;
  do not require every environmental diagnostic to pass to prove correct selection.
  No egress provisioning or host-policy changes are needed for this read-only proof.
- Multiple-provider selection uses deterministic injected contribution boundaries;
  the current live host need not acquire an unrelated remote-provider prerequisite.

## Failure model and bounds

This is selection repair, not a new registration registry. The contribution list
and enabled predicates remain assembly authority. An enabled target's hook failure
retains existing audited assembly-error behavior. Duplicate matching contributions
are retained as before; no new duplicate-name policy is introduced. Provider
selection does not restrict the intentionally provider-independent secret check.
