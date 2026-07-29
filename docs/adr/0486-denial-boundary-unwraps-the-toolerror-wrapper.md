# ADR 0486 — The denial boundary unwraps FastMCP's `ToolError` and answers with a `ToolResult`

- **Status:** Accepted
- **Date:** 2026-07-28
- **Issue:** #1635
- **Makes reachable:** [ADR-0062](0062-platform-operations.md) §5 (a member's role over-reach is
  audited at the one dispatch boundary) and
  [ADR-0098](0098-membership-denial-envelope.md) decision 2 (a membership denial reaches the
  caller as an `authorization_denied` envelope). Both required this boundary; neither could fire.
- **Amends:** [ADR-0485](0485-purpose-keyed-middleware-skip-sets.md), whose account of
  `DenialAuditMiddleware`'s `REENTRANT_TOOLS` skip as *ordering-defensive and unreachable today*
  was written against the pre-fix chain. See Consequences for what survives that change and what
  does not.
- **Amends:** [ADR-0045](0045-spine-driver-capability-grant-phase-naming.md) §2 and
  [ADR-0046](0046-spine-report-phase-accounting-assertions-artifact.md) §3, which codify the
  live-stack driver's **raised**-RBAC path — "a `require_role` denial **raises** (no authz
  `ErrorCategory`), which fastmcp surfaces as a tool error", and the report negative's contrast
  with "the raised-`LiveStackToolError` path the `viewer` operator-op negative" takes. That
  premise is exactly what this ADR falsifies. See Consequences.
- **Activates:** [ADR-0490](0490-denial-names-the-missing-role.md), whose Consequences state that
  its coverage of the ~40 non-locally-catching `require_role` sites is gated on this issue.
- **Takes no position on:** [ADR-0471](0471-denial-envelope-terminal-affordance.md), which
  recorded these two `except` clauses as unreachable and deliberately did not say what to do
  about it.

## Context

`DenialAuditMiddleware` (`src/kdive/mcp/middleware/denial_audit.py`) is the one place where a
denial that no handler caught becomes an audited, enveloped, uniformly-classified answer. It did
that with two `except` clauses — `RoleDenied` and `ProjectMembershipDenied` — around
`call_next(context)`.

Neither clause has ever fired on a real dispatch.

FastMCP 3.4.4 (pinned `==3.4.4`) builds the middleware chain strictly **outside** the branch that
runs the tool. `FastMCP.call_tool` (`fastmcp/server/server.py:1248-1270`) constructs the
`MiddlewareContext` and calls `_run_middleware` with a `call_next` that re-enters `call_tool` with
`run_middleware=False`; only that inner branch resolves and runs the tool, and only it carries the
`except Exception as e: raise ToolError(...) from e` at `:1341-1358`. So by the time any
middleware sees the failure, the denial is a `ToolError` with the real exception demoted to
`__cause__`, and `except RoleDenied` cannot match.

This is the production path in all three shapes:

- a direct `app.call_tool(name, args)`;
- the `tools.invoke` gateway (`src/kdive/mcp/tools/gateway.py`), which itself calls
  `app.call_tool(..., run_middleware=True)` and therefore re-enters the same wrapping branch;
- the SDK `tools/call` handler `_call_tool_mcp`
  (`fastmcp/server/mixins/mcp_operations.py:235`) that every MCP client and every curated
  `kdivectl` verb (`src/kdive/cli/dispatch.py`) reaches the server through.

What that cost, measured against a real migrated Postgres with a `viewer` on `proj-a` calling
`audit.query {scope: project, project: proj-a}` (which requires `admin` and deliberately re-raises
`RoleDenied` so the boundary can audit it):

| plane | before | ADR-0062 §5 / ADR-0098 require |
|---|---|---|
| `audit_log` | no row | one row keyed to the tool, `transition='denied'` |
| envelope | `ToolError` out of `call_tool` | `authorization_denied`, `detail` `"access denied"` |
| `tool_invocation.outcome` | `error` | `denied` |
| `kdivectl` exit | 1 (generic) | 3 (`authorization_denied`, `kdive/cli/errors.py`) |

The `ProjectMembershipDenied` arm was equally dead; it looked healthy only because the tools that
exercise it — `audit.query` among them — catch `AuthorizationError` in the handler and build the
envelope there.

No test caught this because every test of this middleware called `mw.on_call_tool` directly with a
hand-built context and a `call_next` that raised the denial. That is the shape the middleware was
written for, and it is not the shape it is ever given.

## Decision

### 1. Classify the denial off `ToolError.__cause__`, and only the immediate cause

`on_call_tool` gains an `except ToolError` arm. `_wrapped_denial` reads `exc.__cause__` and
returns it when it is a `RoleDenied` or a `ProjectMembershipDenied`, and `None` otherwise; on
`None` the `ToolError` is re-raised unchanged, so an ordinary failure — or a `DestructiveOpDenied`
or a `require_platform_role` denial, both of which their own handlers already audit — is not
swept in and double-written.

This is the mechanism kdive already uses at its other two wrapper seams, not a second one:
`gateway.py:271-274` (`except ToolError` / `isinstance(exc.__cause__, CategorizedError)` / else
re-raise) and `binding_errors.py:154` (`except FastMCPValidationError` / `isinstance(cause,
ValidationError)` / else re-raise). Both read the **immediate** cause, and so does this.

Rejected: **walking the `__cause__` chain to its root.** It would resurrect a denial a handler
deliberately converted with `raise Other from denial`, reporting `authorization_denied` for a
failure the handler decided was something else. The depth it would buy is unreachable anyway: the
only way to nest a second `ToolError` layer is for one to escape an inner middleware chain, and
after this ADR every denial is enveloped by the inner chain before it can.

Rejected: **catching the denial in each of the ~40 `require_role` sites instead.** That is the
same fix written 40 times, drifts immediately, and abandons the single-boundary property ADR-0062
§5 depends on — the audit row exists precisely because there is one place that writes it.

Rejected: **unwrapping in `gateway.py` alone.** It is one of three entry points, and the direct
and SDK paths would keep raising.

### 2. The short-circuit returns a `ToolResult`, not a bare `ToolResponse`

The two arms returned `ToolResponse.denied(...)` — a bare pydantic model. `_call_tool_mcp` calls
`result.to_mcp_result()` on whatever `call_tool` returns, and only `ToolResult` implements that
method. A bare `ToolResponse` reaches the client as
`ToolError: 'ToolResponse' object has no attribute 'to_mcp_result'`.

That defect was latent for exactly as long as the arms were dead, and decision 1 is what would
have shipped it. `_denied_result` now wraps the envelope
(`ToolResult(structured_content=envelope.model_dump(mode="json"))`) — the same shape `gateway.py`
and `BindingErrorMiddleware` already return — so the denial survives the transport and
`kdivectl` maps it to exit 3 instead of exit 1.

Rejected: **teaching `ToolResponse` a `to_mcp_result` method.** It would make the response model
depend on the MCP transport to work around one call site that already has a correct type for the
job.

### 3. Which denial is audited is unchanged, and the role-less arm now *states* its answer

`RoleDenied` is audited and names `denial.required` under `data.missing_roles` (ADR-0490).
`ProjectMembershipDenied` is enveloped without an audit row and without a role: it is the
non-member case, excluded from auditing to avoid write-amplification on openly-callable reads
(ADR-0043 §4, ADR-0098), and naming a role there would confirm the project exists (ADR-0123).
Both arms now route through one `_denied` method, which branches on the class, and through one
`_denied_result` constructor; the two policies are the same policies, in one place instead of two.

That collapse changes which of ADR-0490 §4's two shapes this module has. It no longer contains a
role-less `ToolResponse.denied` call, so its entry leaves `_ROLELESS_DENIALS` in
`tests/guards/test_denial_envelope_actions.py`; instead `_denied_result` takes the role as a
**required but nullable** parameter and joins `_ROLE_FORWARDING_HELPERS`, the same shape
`images/_common.denied` already uses. The membership arm passes an explicit `None` — a stated
"no role would have helped" rather than an omitted argument — and the guard now fails if a future
arm gives that parameter a default or stops forwarding it, which is the enforcement ADR-0490 §4
said the exemption depends on. ADR-0490's closing note that `missing_roles` could not be made
required on `ToolResponse.denied` because this module legitimately passes no role still stands:
the requirement is enforced one layer in, at this module's own constructor.

### 4. The pin is a real-dispatch test, not another direct `on_call_tool` call

Every pre-existing test of this middleware stays green through the whole regression, so a unit
test cannot be this decision's evidence. The pin is
`tests/mcp/tools/test_gateway_usage_recording_e2e.py`: a real `build_app` over a real migrated
Postgres and a real verified token, driving the denial through `app.call_tool` both directly and
through `tools.invoke`, asserting all three planes together — envelope (including
`data.missing_roles == ["admin"]`), `audit_log`, and `tool_invocation.outcome`. A fourth test
drives the same denial through a real `Client`, because `app.call_tool` returns the middleware's
object as-is and only the transport exercises decision 2.

## Consequences

**ADR-0485's characterisation of this middleware's skip arm needs a follow-on note.** ADR-0485
recorded `DenialAuditMiddleware`'s `REENTRANT_TOOLS` check as ordering-defensive and unreachable,
reasoning that the inner instance *returns* an enveloped denial rather than re-raising, so no
`RoleDenied` can reach an outer instance. That conclusion still holds — this ADR makes it hold for
the first time. Before this change the reasoning was accidentally right for the wrong reason: the
inner instance did not envelope anything, and the arm was unreachable because a `ToolError`
reached the outer instance and matched no clause there either. The comment in `_record` is updated
to state the reachability as a consequence of this boundary rather than as a standing fact, and
the claim that "no audit-row loss is claimed for the denial arm" is now true because the arm
writes rows at all.

**ADR-0490 goes live.** Its Consequences state that its coverage is gated on this issue: only five
modules catch `RoleDenied` locally, and the other ~40 `require_role` sites — allocations, runs,
systems, `ssh_access`, debug sessions, vmcore, artifacts, accounting — funnel here. Those denials
now name the role they required. The disclosure ADR-0490 accepted, bounded to a closed enum of
seven tokens, takes effect across that surface at the moment this lands, and the real-dispatch
test above is what proves the role survives the unwrap rather than being reconstructed from the
wrapper's message.

**Denial rates stop being undercounted.** `UsageTrackingMiddleware` runs outside this middleware,
so it now classifies the returned envelope as `denied` instead of catching a `ToolError` and
recording `error`. Any dashboard or query that read `tool_invocation.outcome` will see denial
counts rise and error counts fall by the same amount at the changeover; neither the table nor its
vocabulary changes.

**`UsageTrackingMiddleware`'s own `except AuthorizationError` arm remains unreachable on the real
dispatch path, for the identical reason, and this ADR does not fix it.** A denial class this
boundary does not own (a bare `AuthorizationError`, `DestructiveOpDenied`) that also escapes its
handler would still be recorded `error`. Every such class is enveloped by its handler today, so
there is no observed loss; the arm is a defensive branch whose premise this ADR shows to be false,
and it is left for a separate decision rather than widened here by inference.

**Every caller that branched on the transport's `is_error` flag alone now reads a denial as a
success.** This is the widest-reaching consequence of the change, and the repo had two such
callers.

`scripts/kdive_set_accounting.py` was one, and its contract test caught it: a `viewer` running
the onboarding helper used to exit 1 on the first denied call and now ran to completion and
exited 0, having written nothing. It is fixed here to treat a returned failure envelope as a
failure, and keeps exiting 1 rather than adopting `kdive/cli/errors.py`'s mapped 3, so **its
exit codes are unchanged at every outcome** — 0 success, 2 no token, 1 any failure. Its *stderr*
is not identical: a failure now also dumps the envelope, which is new diagnostic output on the
enveloped path. The dump is suppressed when `structured_content` is `None`, so the pre-existing
`is_error` path does not gain a bare `null` line.

`LiveStackClient.call_tool` (`src/kdive/mcp/dev_harness.py`) was the other, and **no gate in
`just ci` could have caught it**: `just test` excludes the `live_stack` marker, so the driver
that consumes it is never run there. Its viewer negative
(`tests/integration/test_live_stack.py::test_viewer_denied_operator_op_over_the_wire`) asserted
`pytest.raises(LiveStackToolError)` on `allocations.request`, which calls
`require_role(ctx, project, Role.CONTRIBUTOR)` and does not catch it — the one wire test that
reaches this middleware. That denial is now an envelope, so the assertion inverts. The test is
rewritten to assert the envelope and, additionally, ADR-0490's named `contributor`, which makes
it the live-tier proof of both ADRs; the harness's own docstrings, which cited an authz denial as
the example of the raising shape, are corrected.

Because that tier cannot gate this change, its behaviour is instead made predictable from a
normal CI run: `test_role_denial_reaches_the_live_stack_harness_as_an_envelope` drives the real
`LiveStackClient` over a real `Client` against a real denial. It joins the two halves that would
otherwise never meet — that a denial produces `is_error=False`, and that `is_error=False` takes
the envelope-parsing branch (covered only over a fake client).

**That harness change falsifies a premise ADR-0045 §2 and ADR-0046 §3 state as fact**, which is
why they are amended above rather than merely cited. ADR-0045 §2 introduced `LiveStackToolError`
on the stated grounds that a `require_role` denial *raises* and so cannot be asserted as an
envelope; ADR-0046 §3 then justified the report negative's envelope assertion **by contrast**
with that raised path, and listed "assert the denial as a raised `LiveStackToolError`" among its
rejected alternatives on the strength of that contrast. After this ADR the contrast is gone:
every RBAC negative on the driver asserts an envelope, and `LiveStackToolError` covers only a
genuine server fault carrying no typed category. Neither ADR's *decision* is reversed — the typed
error still exists and still wraps the raising shape, and ADR-0046's rejection stands and in fact
widens from wrong-for-this-tool to wrong-for-every-tool — but the example each used to motivate
it is no longer an instance of it. Both files therefore carry an in-place `Amended by` note at
the falsified passage, following ADR-0082 (amended by ADR-0489) and ADR-0268 §6 (amended by
ADR-0485): a reader who lands on a 440-ADR-old file directly must not be misled, and a note the
amending ADR alone carries is only discoverable from the wrong end.

A sweep of all non-test `src/` and `scripts/` found no third caller of this pattern, and a
sweep of `tests/` found no affected `is_error` branch outside the live-stack family: the
remaining ones are input-schema rejections (ADR-0147), binding failures on an app built without
this middleware, or fixtures that construct the flag directly. Any out-of-repo consumer with the
same pattern has the same gap; it is ADR-0089's returned-envelope shape and not new, but a denial
joining that set is what makes it bite.

**`CompactResponseMiddleware`'s bare-`ToolResponse` branch is removed, not left inert.**
`_compact_result` handled both a `ToolResult` and a bare `ToolResponse`, citing this middleware's
short-circuit as the reason the second shape existed. After decision 2 nothing produces it. It is
deleted rather than kept as a fail-safe because it never was one: it could only run with
`KDIVE_COMPACT_RESPONSES` **on**, and with the flag off — the default — a bare `ToolResponse`
still reached `to_mcp_result()` and died. Keeping it would leave a wire-invalid shape looking
supported in the outermost middleware, which is precisely how the `to_mcp_result` defect this ADR
fixes stayed latent. A test now pins the fail-safe that replaces it: an unrecognised object
passes through untouched and is *not* converted to a `ToolResult`, so a regression fails at the
transport on every path rather than only when compaction happens to be on.

No schema, no migration, no configuration setting, no change to the tool list, and no change to
any tool's arguments or output schema. The only client-visible change is the one ADR-0062 §5 and
ADR-0098 always specified: a role over-reach that previously surfaced as a generic `ToolError`
now surfaces as the uniform `authorization_denied` envelope, with `detail` the ADR-0123 constant.
