# ADR 0493 — The non-member `AuthorizationError` is enveloped at the handler, and the usage plane's dead denial arm is deleted

- **Status:** Accepted
- **Date:** 2026-07-29
- **Issue:** #1661
- **Amends:** [ADR-0486](0486-denial-boundary-unwraps-the-toolerror-wrapper.md), whose Consequences
  state that `UsageTrackingMiddleware`'s `except AuthorizationError` arm "remains unreachable …
  Every such class is enveloped by its handler today, so **there is no observed loss**". The
  unreachability holds. The no-loss claim does not: `accounting.report`'s granted-set branch let a
  base `AuthorizationError` escape, so a real denial reached the client as a raw `ToolError` and
  was metered `error`. See Context.
- **Settles:** the same ADR's "the arm … is left for a separate decision rather than widened here
  by inference." This is that decision: **delete**, not widen. See §2.
- **Takes no position on:** [ADR-0123](0123-tool-error-detail-surfacing.md) and
  [ADR-0490](0490-denial-names-the-missing-role.md), whose disclosure rules this ADR applies
  unchanged — the non-member arm names no role, the member arm names one.

## Context

`require_role` (`src/kdive/security/authz/rbac.py`) raises **two** classes, and the difference is
load-bearing:

| site | class | who owns it |
|---|---|---|
| `project not in ctx.projects` | base `AuthorizationError` | **nobody** |
| member, held role ranks below required | `RoleDenied` | `DenialAuditMiddleware` (ADR-0486) |

`DenialAuditMiddleware`'s `_DENIAL_TYPES` is `(RoleDenied, ProjectMembershipDenied)` on purpose:
ADR-0486 rejected a base-class catch because it would sweep in `require_platform_role` denials and
`DestructiveOpDenied`, which their own handlers already envelope. The consequence is that an
escaping **base** `AuthorizationError` is owned by no boundary at all. It is re-raised as
`ToolError`, and `UsageTrackingMiddleware`'s `except AuthorizationError` cannot match it — FastMCP
builds the middleware chain outside the branch that wraps a tool exception, so the denial arrives
already wrapped. It falls through to `except Exception` and records `ToolOutcome.ERROR`.

ADR-0486 disclosed that arm as unreachable and asserted no loss was observed, on the reasoning that
every class reaching it is enveloped by its own handler. #1661 filed that as an inference with a
named hole. A site-by-site audit of all 52 `require_role` calls, 28 `require_platform_role` calls,
and the single `DestructiveOpDenied` raiser closes it:

- **51 of 52** `require_role` sites cannot reach it. 38 sit behind an inline
  `if <row>.project not in ctx.projects: return not_found` guard, so the project is resolved from an
  already-scoped row; 7 call `require_project(...)` first, which raises the enveloped
  `ProjectMembershipDenied`; 5 catch `AuthorizationError` locally; 3 iterate `ctx.projects`
  themselves.
- **28 of 28** `require_platform_role` sites are the first statement of a `try` whose next line is
  `except AuthorizationError`.
- `DestructiveOpDenied` has one raiser (`security/authz/gate.py`) and one catcher
  (`lifecycle/control/registrar.py`), which envelopes it.
- **1 site is reachable**: `_resolve_granted_set` in `src/kdive/mcp/tools/accounting/reports.py`.
  It hands a **caller-named** project list straight to `require_role(ctx, project, VIEWER)` with no
  membership pre-guard, and nothing up its call chain catches. `report_granted_set`'s one `try`
  wraps only `_parse_group_by` / `_parse_window` and catches only `CategorizedError`; the resolver
  is invoked from `_report_granted_set`, outside it.

Measured against a real migrated Postgres with a `viewer` on `proj-a` calling
`accounting.report {scope: "granted-set", projects: ["proj-not-granted"]}`:

| plane | before | required |
|---|---|---|
| envelope | `ToolError` out of `call_tool` | `authorization_denied` (ADR-0098) |
| `tool_invocation.outcome` | `error` | `denied` (ADR-0148) |
| `kdivectl` exit | 1 (generic) | 3 (`kdive/cli/errors.py`) |

This is the outlier, not a design: its structural twin `_resolve_granted_targets` in
`src/kdive/mcp/tools/reports/generate.py` has the arms; `accounting/reports.py` never grew them.

## Decision

### 1. `accounting.report`'s granted-set branch envelopes the base class and re-raises `RoleDenied`

`_report_granted_set` wraps its `_resolve_granted_set` call in two arms:

```python
except RoleDenied:
    raise
except AuthorizationError:
    return ToolResponse.denied(_REPORT_OBJECT_ID)
```

The order matters and so does the bare `raise`. `RoleDenied` subclasses `AuthorizationError`, so a
single `except AuthorizationError` would swallow **both** — and with `RoleDenied` swallowed,
`DenialAuditMiddleware` never sees it and ADR-0062 §5's `audit_log` row is silently lost. The
member over-reach must keep propagating to the one boundary that writes that row. The non-member
denial is not audited at all (ADR-0043 §4, ADR-0098: auditing it would let any authenticated token
amplify writes on an openly-callable read), so enveloping it locally loses nothing.

No role is named on the non-member arm. `viewer` there would confirm `proj-z` exists and is merely
not granted, which is what ADR-0123's seam exists to prevent; `RoleDenied` is safe to name because
it fires only for a member (ADR-0490), and the boundary names it.

This is not a new shape. `_require_job_role` (`src/kdive/mcp/tools/jobs.py`) is exactly this, arm
for arm, and this ADR follows it.

**Rejected: mirroring `reports/generate.py`'s three arms**, which add `except RoleDenied: return
ToolResponse.denied(..., missing_roles=[exc.required])`. It produces a near-identical envelope, so
it looks equivalent — but it envelopes the member over-reach *locally* and therefore drops the
`audit_log` row ADR-0062 §5 requires. That `generate.py` already does this is disclosed below as an
adjacent defect rather than copied.

**Rejected: widening `DenialAuditMiddleware`'s `_DENIAL_TYPES` to the base `AuthorizationError`.**
It would fix every future site at one boundary, which is the argument for it. Against: ADR-0486
chose the closed tuple deliberately, and widening re-opens it — a `require_platform_role` denial or
a `DestructiveOpDenied` that a handler catches and converts with `raise Other from denial` would be
resurrected as `authorization_denied`. It also changes behaviour across the whole tool surface to
fix one site the audit above bounds at one.

**Rejected: a membership pre-guard** (`if project not in ctx.projects: return not_found`), the
shape 38 other sites use. Those resolve a project from a **row** the caller may not know exists, so
`not_found` is the honest answer. Here the project is a *name the caller typed*; answering
`not_found` would make `accounting.report` a project-existence oracle, and the caller has not asked
about an object at all.

### 2. `UsageTrackingMiddleware`'s `except AuthorizationError` arm is deleted, not widened

With §1 landed the arm is dead by construction: every denial class is enveloped before this
middleware sees a result, and FastMCP cannot deliver an unwrapped `AuthorizationError` to it in any
case. The repo's standing rule is to remove a replaced implementation rather than leave a shim, and
a defensive branch whose premise is false is worse than absent — it is what made ADR-0486's "no
observed loss" reasoning look safe.

**Rejected: widening it to `except ToolError` + an `exc.__cause__` unwrap**, the mechanism ADR-0486
decision 1 used one middleware in. It would set `tool_invocation.outcome = 'denied'` for an escaping
denial, which is the metric #1661 opens with — but it would do so while the *client* still receives
a raw `ToolError`. That is strictly worse than the status quo it replaces: the table would report a
clean denial for a call the caller experienced as a server error, and the divergence between the two
planes is exactly the signal an operator would need to find the next site. The outcome column
should follow the envelope, not paper over its absence. Fixing the handler repairs both planes at
once; widening repairs the cheaper one and hides the other.

The `_classify` path is untouched: a denial is read off the returned envelope's `error_category`,
which is how every denial has actually been classified since ADR-0486.

### 3. The pin is a real-dispatch test, and the vacuous ones are removed

Three tests in the affected modules were the direct-`on_call_tool` shape ADR-0486 §4 disqualifies —
a hand-built context and a `call_next` that raises. All three were green before this change and
after it, against both the defect and its fix, so none of them was evidence of anything.

`tests/mcp/tools/test_gateway_usage_recording_e2e.py` gains two tests over a real `build_app`, a
real migrated Postgres, and a real verified token. The first drives the non-member denial and
asserts all three planes together — envelope (with `missing_roles` **absent**), `tool_invocation`
row `denied`, and `audit_log` empty. It reddens against the unfixed handler by raising out of
`call_tool` before it can assert anything, and the row it does leave behind says `error`, which was
confirmed by probe before the fix was written.

The second is what makes §1's two-arm catch load-bearing rather than incidental: a **role-less
member** naming their own project must still reach the boundary. It asserts the `audit_log` row and
the boundary's tool-keyed `object_id`, both of which vanish the moment `RoleDenied` stops
propagating. Without it, collapsing the two arms into one would land green.

Removed, with an in-place note saying what stood there and why nothing replaced it:

- `test_usage.py::test_on_call_tool_records_denied_and_reraises_on_authorization_error` — pinned
  the deleted arm. The envelope-classified denial it appeared to cover is already pinned twice over
  by `test_classify_denied_for_authorization_denied` and
  `test_on_call_tool_records_classified_outcome_on_success`, so replacing it would only add a third.
- `test_usage_tracking_middleware.py::test_denied_from_propagated_authorization_error` — raised
  `DestructiveOpDenied` from `call_next`. Its class has one raiser and one catcher, and the catcher
  envelopes it before any middleware runs.

That module's docstring claimed coverage of "a propagated `AuthorizationError` … that bubbles past"
the boundary and of "a bare `ToolResponse` on its short-circuit". The first is now false by §2; the
second was falsified by ADR-0486 decision 2 and was not corrected then. Both are rewritten to state
what the module actually covers, including why `test_denied_from_bare_toolresponse` survives —
`result_error_category` is a shared helper that accepts both shapes, not a live wire format.

## Consequences

**`accounting.report` gains no argument, no field, and no new outcome vocabulary.** A caller naming
a project they are not a member of receives the same `authorization_denied` envelope every other
denial on the surface returns, with ADR-0123's constant `detail` and no `missing_roles` key. The
only clients that change behaviour are those that treated the call as a transport-level fault:
`kdivectl` moves from exit 1 to exit 3, and any caller branching on `is_error` alone now reads the
denial as a returned envelope — the ADR-0486 consequence, reaching one more tool.

**Denial counts rise and error counts fall for `accounting.report`, by the same amount.** Neither
`tool_invocation` nor its outcome vocabulary changes. The shift is confined to the one tool; the
audit above is what bounds it.

**`reports/generate.py`'s local `except RoleDenied` arm drops an `audit_log` row, and is left
alone.** It catches the member over-reach and envelopes it in the handler, so
`DenialAuditMiddleware` never sees it and ADR-0062 §5's row is not written for
`reports.generate`'s granted-set branch. That is a real defect and it is disclosed here rather than
fixed, because repairing it changes a second tool's behaviour on a path #1661 does not name and
which has its own tests to re-baseline. It is filed as follow-on work. Its envelope is not wrong —
`missing_roles` is populated identically — so the loss is the audit row alone.

> **Retracted by [ADR-0508](0508-reports-generate-re-raises-the-member-over-reach.md)
> (2026-07-30, #1680).** The follow-on work landed: that arm is now a bare `raise`, the member
> over-reach reaches this boundary, and the row is written. The disclosure above holds only for
> the state of the tree between #1661 and #1680. Two consequences of this record change with it.
> The rejected alternative in §1, "mirroring `reports/generate.py`'s three arms", is moot — the
> three-arm shape no longer exists anywhere, and `generate.py` now uses the two-arm shape this
> ADR chose. And "its envelope is not wrong" turns out to be true in every field but one:
> `object_id` moves from the handler's `"report"` to the boundary's `"reports.generate"`,
> the same shift `accounting.report` took here. ADR-0508 measures both envelopes and records the
> diff.

**The 52-site audit is a point-in-time result, not an invariant.** Nothing in `just ci` fails a new
`require_role` call on a caller-named project with no guard and no catch; the next one reaches the
client as a raw `ToolError` exactly as this one did, and with §2 landed there is no longer even a
metering arm pretending otherwise. A structural guard over that shape is the durable answer and is
deliberately out of scope here — it needs to distinguish a caller-named project from a
row-resolved one, which is a dataflow property, not a grep. Recorded so the next reader knows the
audit is the evidence for *this* change and not a standing guarantee.

> **Superseded by [ADR-0507](0507-structural-guard-over-the-bare-authorization-error.md)
> (2026-07-30, #1681).** The guard now exists:
> `tests/guards/test_require_role_membership_guard.py` fails any `require_role` that can reach its
> non-member arm with no membership check and no handler that envelopes the base
> `AuthorizationError`, and pins the same property for `require_platform_role`. It sidesteps the
> dataflow problem named above rather than solving it — it requires a mitigation at *every* call
> site instead of deciding which projects are caller-named, because the premise that a
> row-resolved project is safe turns out not to hold, and the row-resolving tools already carry
> the membership check that satisfies the rule. The residual that remains is the analysis's own
> boundary: it is intra-procedural plus one level of intra-module call graph, and the sites it
> cannot resolve are listed with a prose reason rather than passed silently. ADR-0507 records what
> it can and cannot see.

**`doc_exposure.py` raises a bare `AuthorizationError` too**, on `on_read_resource`. That is the
resources plane, which `UsageTrackingMiddleware` does not hook and `DenialAuditMiddleware` does not
see, so it is unaffected by both decisions here. Noted because a future reader weighing "envelope
`AuthorizationError` broadly" will find it, and it is a second raiser on a plane neither boundary
covers.

No schema, no migration, no configuration setting, no change to the tool list, and no change to any
tool's arguments or output schema.
