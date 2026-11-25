# Preserve image authorization denials when auditing fails

## Problem and outcome

The image tools await their local project-denial audit before returning a denial. A failed
connection or write escapes instead. The ordinary denial middleware already keeps audit
failures from replacing its response. Apply that policy at the existing image helper.

## Scope

Keep `audit_project_denial(pool, ctx, *, tool, project, args) -> None`. Catch `Exception`
around its entire connection/transaction block. Emit one warning with fixed text and tool,
without explicit principal/project/session/args, raw exception text, traceback or stack.
Do not change global logging context enrichment. Propagate `BaseException`, including cancellation.
Keep response shaping in the callers; add no wrapper or retry. The existing member denial
still names operator and the original image ID/name. Non-member and foreign-entry concealment,
admin auditing and successful mutation auditing retain their policies.

[ADR-0856](../../adr/0856-image-denial-audit-is-best-effort.md) records this bounded failure policy.
The helper remains the owner; no import migration or new runtime seam is required. Change
only the helper and existing image-tool test module, with internal audit wording corrected
only if needed. Public parameters, row/schema formats and SQL stay unchanged.

### Failure model and threat model

- Actors: authenticated callers with insufficient project roles; unavailable or faulty audit
  connections/transactions/writer. Assets: denied mutation, stable error envelope, concealment
  and audit integrity when storage works. The audit failure must not enter an upload/delete path.
- Handle ordinary failures calling/entering/exiting the audit connection, entering/exiting its
  transaction, or writing the denial. Keep the attempted write in its own existing transaction.
- Reject leaking a sensitive exception payload through the new warning's explicit fields.
  Existing global processors/context policy are unchanged; no generic logging overhaul.
- Accept that an exit failure can leave audit commit outcome unknown. No retry, durable recovery,
  audit availability guarantee or suppression of cancellation/process termination is introduced.
  Delete's earlier catalog lookup remains outside this audit-only policy.

## Validation and completion

Mode: focused-test. Add both-handler/six-boundary cases to `tests/mcp/ops/test_images_tools.py`.
Run them before implementation: expect twelve raised injected audit failures, not setup errors.
Afterward require original object ID, authorization_denied, operator missing role, one safe
warning, no upload store/service access and unchanged catalog row. Use the real disposable
Postgres and existing fixtures; patch only the audit boundary after delete's lookup. Add a
helper cancellation case and preserve the existing successful audit and concealment tests.
Run the entire image-tools file and denial-middleware/role-envelope regressions, lint,
whole-tree types, docs checks and hooks. Fresh bounded code/security review precedes the local
conventional commit; resolve and attribute only the exact finding after proof.

## Global Constraints

Python3.14/uv and existing dependencies. Linux host x86_64; declared targets x86_64 and ppc64le.
No live hardware/stack claim. Keep changes and commits local. Final whole-campaign CI and
whole-branch reviews remain the enclosing run's completion gate.
