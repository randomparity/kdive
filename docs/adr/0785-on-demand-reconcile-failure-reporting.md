# 0785 Report on-demand reconciliation authority failures

## Status

Accepted (2026-10-16)

Implements the operator-approved reporting choice for issue #3239.

## Context

On-demand reconciliation executes the periodic plan through the server pool. An actual
server LOGIN against disposable Postgres reproduces all nine reported permission failures;
an actual reconciler LOGIN has none on the same empty-state plan. The tool previously
returned success even when repair kinds raised. Issue #3239 permits accurate reporting.

## Decision

Keep repair execution and database authority unchanged. Preserve failure names and partial
counts, add bounded per-kind reason/recovery metadata, and return `infrastructure_failure`
when any isolated repair raises. Permission denial names the periodic reconciler as recovery;
unknown failure requires operator inspection. Expose no raw exception data in that metadata.

## Consequences

A server-role pass can report an error even though sibling repairs succeeded. Counts can
undercount work committed before a repair raised. Callers must inspect failures rather than
blindly retry. A recovery code identifies the authority owner, not a guarantee of repair.
Existing whole-pass handling and periodic scheduling remain unchanged.

## Considered & rejected

- **Keep success with failure names.** judgment: a success envelope hides incomplete repairs
  and gives no actionable explanation.
- **Expand server grants or inject reconciler credentials.** judgment: broadens request-path
  authority to solve a reporting requirement and crosses the approved authority boundary.
- **Queue a dedicated reconciliation request.** judgment: adds persistence, dispatch and
  recovery contracts beyond this bounded reporting fix.
