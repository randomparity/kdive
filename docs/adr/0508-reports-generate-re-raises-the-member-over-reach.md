# 0508 — `reports.generate` re-raises the member over-reach so the boundary audits it

## Status

Accepted (2026-07-30)

## Context

ADR-0062 §5 puts the denial `audit_log` row at exactly one place: the MCP tool-dispatch
boundary, which "catches `RoleDenied` specifically … records a denial row, and re-raises".
`middleware/denial_audit.py`'s `DenialAuditMiddleware` is that boundary. It sees a denial only if
the denial keeps propagating out of the handler, so a handler that catches `RoleDenied` and
builds the envelope itself produces a correct answer to the caller and no audit row at all.

`reports.generate`'s granted-set branch did exactly that:

```python
except RoleDenied as exc:
    return ToolResponse.denied(_REPORT_OBJECT_ID, missing_roles=[exc.required])
except AuthorizationError:
    return ToolResponse.denied(_REPORT_OBJECT_ID)
```

`_resolve_granted_targets` calls `require_role(ctx, project, Role.VIEWER)` once per project the
caller named. A caller who **is** a member of a named project but holds no role on it reaches
`require_role`'s rank-below site, which raises `RoleDenied` — the class ADR-0062 §5 audits — and
the first arm swallowed it. The result was a correctly-shaped denial with `missing_roles` present
and no row in `audit_log`. "Who was denied on project X" was unanswerable for this one tool.

ADR-0493 found this while fixing #1661 on `accounting.report` and deliberately left it alone,
disclosing it in its Consequences as a real defect and filing it as follow-on work (#1680). It
also recorded **"mirroring `reports/generate.py`'s three arms"** as a rejected alternative for
that fix, precisely because copying this shape would have inherited the dropped row.

The defect is bounded to this one handler. All seven `RoleDenied` handlers on the tool surface
were re-checked at the time of this decision:

| handler | shape | audited? |
|---|---|---|
| `mcp/tools/jobs.py:178` | re-raises | yes, at the boundary |
| `mcp/tools/accounting/reports.py:146` | re-raises (ADR-0493) | yes, at the boundary |
| `mcp/tools/ops/audit/audit.py:200` | re-raises | yes, at the boundary |
| `mcp/tools/ops/images/delete.py:51` | envelopes locally | yes, writes its own row first |
| `mcp/tools/ops/images/upload.py:71` | envelopes locally | yes, writes its own row first |
| `mcp/tools/lifecycle/systems/admin.py:369` | envelopes locally | yes, writes its own row first |
| `mcp/tools/reports/generate.py:323` | envelopes locally *(the one this record changes)* | **no** |

`services/investigations/lifecycle.py:132` also catches `RoleDenied`, but converts it into an
`InvestigationServiceError`. That is a service-layer domain conversion below the tool surface,
not a denial the boundary was ever going to see, and it is out of scope here.

## Decision

`reports.generate`'s granted-set branch re-raises `RoleDenied` and lets `DenialAuditMiddleware`
both audit and envelope it. The non-member arm is untouched and still envelopes the base
`AuthorizationError` locally, naming no role.

```python
except RoleDenied:
    raise
except AuthorizationError:
    return ToolResponse.denied(_REPORT_OBJECT_ID)
```

This is `jobs.py::_require_job_role`'s shape, arm for arm, and the one ADR-0493 adopted for
`accounting.report`. The order matters and so does the bare `raise`: `RoleDenied` subclasses
`AuthorizationError`, so a single base-class arm would swallow both and lose the row again.

**The pin is a real-dispatch test, and it has to be.** `tests/mcp/tools/reports/test_generate.py`
calls `generate` directly, below every middleware, so it cannot observe a row the boundary
writes — a direct-call test would have stayed green across this entire change in either
direction. `tests/mcp/tools/test_gateway_usage_recording_e2e.py` gains two tests over a real
`build_app`, a real migrated Postgres and a real verified token:

- `test_roleless_member_named_project_generate_is_audited_at_the_dispatch_boundary` asserts the
  `audit_log` row, the boundary-keyed `object_id`, and `missing_roles`. Reverting the handler
  arm leaves `audit_log` empty and reddens it; that was confirmed by running it against the
  reverted tree before this record was written.
- `test_non_member_named_project_generate_still_envelopes_at_the_handler` is the control that
  bounds the change to the subclass. Without it, "the `RoleDenied` arm re-raises" and "the whole
  `except` block re-raises" are indistinguishable.

The direct-call test that asserted the old envelope is re-baselined to assert the propagating
`RoleDenied` — the only thing that layer can honestly observe — and says in its docstring where
the envelope and the row are pinned instead.

## Consequences

**The envelope changes in exactly one field: `object_id`, `"report"` → `"reports.generate"`.**
Measured, not asserted: the same call was driven through a real dispatch against both trees and
the two envelopes diffed. `missing_roles: ["viewer"]`, `detail: "access denied"`,
`error_category`, `status`, `retryable`, `items`, `refs` and `suggested_next_actions` are
byte-identical. The shift is the boundary's signature — it is object-agnostic by contract
(ADR-0062 §5) and keys its envelope to the call it intercepted, where the handler keyed to the
object it would have returned. `accounting.report` took the identical shift in ADR-0493 and the
two tests in that module assert the difference rather than normalising it away.

**`missing_roles` survives the move, and that is a property of the boundary, not a coincidence.**
`_denied_result` reads `denial.required` off the same `RoleDenied` the handler used to read it
off, so ADR-0490's disclosure rule is applied by the boundary exactly as the handler applied it.
The role is safe to name for the same reason it always was: `RoleDenied` fires only past the
membership check, so naming it confirms nothing the caller's own membership did not.

**A member over-reach on `reports.generate` now writes one `audit_log` row.** Denial volume on
this tool is a member naming their own role-less project, which is rare; there is no
write-amplification concern of the kind ADR-0043 §4 excludes the non-member case for, because
the non-member arm still does not reach the boundary.

**`tool_invocation` is unchanged.** The call was already recorded `denied` — the outcome is read
off the returned envelope's `error_category`, and the boundary returns the same category the
handler did. Only `audit_log` gains a row.

**ADR-0493's disclosed residual is retracted**, and its rejected alternative is now moot: the
shape it declined to mirror no longer exists. A block quote on that record points here.

**This does not generalize into a rule.** Three handlers on the table above envelope `RoleDenied`
locally and are correct, because each writes its own audit row before doing so. The invariant is
"a member over-reach produces an audit row", not "a handler must re-raise". Nothing in `just ci`
checks that invariant; ADR-0507's guard covers the *non-member* arm's envelope, which is a
different property. A structural guard over the audit row would have to relate a `raise` or a
local `audit.record_denial` to each `except RoleDenied`, and is not attempted here.

## Considered & rejected

- **Write the `audit_log` row in the handler and keep enveloping locally**, the shape
  `ops/images/delete.py` and two siblings use. It keeps `object_id` at `"report"`, which is the
  only thing the re-raise costs. Against: it duplicates ADR-0062 §5's one row-writing site into a
  second place for no gain, and the three handlers that do it have a reason this one lacks —
  they envelope because they have already committed to their own audit shape for the operation.
  It also leaves the tool as the odd one out among the granted-set readers, where `jobs.py`,
  `accounting.report` and `audit.query` all re-raise.
- **Widen `DenialAuditMiddleware`'s `_DENIAL_TYPES` to the base `AuthorizationError`** so the
  handler could drop both arms. Rejected for the reason ADR-0493 rejected it and ADR-0486 chose
  the closed tuple: it resurrects denials a handler deliberately converted with
  `raise Other from denial`, and changes behaviour across the whole surface to fix one site.
- **Preserve `object_id: "report"` by teaching the boundary to read a handler-declared object
  id.** It would make the envelope byte-identical, which is the strongest argument available
  here. Against: it adds a registration surface to a boundary whose whole contract is being
  object-agnostic, to serve a field no denial-handling client branches on — `error_category` and
  `data.missing_roles` are what a caller reads, and both are unchanged. `accounting.report`
  already took this shift and nothing downstream noticed.
- **Fix `services/investigations/lifecycle.py:132` in the same change.** It is the one remaining
  `RoleDenied` catch that does not reach the boundary. Rejected as a different defect class: it
  converts the denial into a domain error deliberately, at the service layer rather than the tool
  surface, so "re-raise it" is not the answer and deciding what is would need its own audit.
- **Pin the fix with a direct handler call asserting `pytest.raises(RoleDenied)` alone.** Cheap,
  fast, and no Postgres. Rejected because it proves the arm re-raises and nothing about the row —
  the row is the entire defect, and only a real dispatch can see it. The direct-call assertion is
  kept, but as a re-baseline of the test that was already there, not as the pin.
