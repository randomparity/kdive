# ADR 0499 — The resources plane answers a gated doc read as not-found, not with a denial envelope

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** kdive maintainers
- **Issue:** #1682
- **Resolves:** [ADR-0493](0493-envelope-the-non-member-denial-at-the-handler.md)'s final Consequences
  bullet, which disclosed `doc_exposure.py`'s bare `AuthorizationError` as "a second raiser on a
  plane neither boundary covers" and left it for a separate decision. This is that decision.
- **Bounded by:** [ADR-0098](0098-membership-denial-envelope.md), whose "Scope of the ADR-0020
  supersession" confines the `authorization_denied` envelope to project-authorization denials
  surfaced **as tool responses**, and which explicitly keeps authentication failures as hard errors
  rather than envelopes. This ADR reads that scope as excluding `resources/read` and states so.
- **Applies:** [ADR-0097](0097-not-found-conflict-error-categories.md)'s no-leak invariant — an
  object the caller may not see takes the *same* answer as one that does not exist — to a plane it
  did not name.
- **Takes no position on:** [ADR-0490](0490-denial-names-the-missing-role.md). Naming a missing
  grant presupposes an envelope with a `data` map; this plane has neither, so `missing_roles` has
  no representation here and none is invented.

## Context

`DocExposureMiddleware` (ADR-0284) gates the `audience="operator"` doc subset on two hooks.
`on_list_resources` **drops** those docs for a caller holding no platform role.
`on_read_resource` raised a bare `AuthorizationError` for the same doc.

#1682 filed this as a missing envelope. Two of its three premises do not survive contact with the
code, and recording why matters more than the fix:

**The issue blames `DenialAuditMiddleware._DENIAL_TYPES` for excluding the base class. That is not
the mechanism.** `denial_audit.py` and `usage.py` implement `on_call_tool` **only**;
`on_read_resource` exists nowhere but `doc_exposure.py`. No `_DENIAL_TYPES` widening, and no choice
of denial class, changes anything on this plane — the boundaries never run. The plane is uncovered
by hook topology, not by a tuple's contents.

**An `authorization_denied` envelope is unrepresentable here.** `ToolResponse` reaches a client
because `_call_tool_mcp` calls `to_mcp_result()` on a `ToolResult` (ADR-0486). The resources
equivalent is `ReadResourceResult`, which carries only `contents`. An envelope returned from
`on_read_resource` either dies at `to_mcp_result` — ADR-0486's exact failure mode, an
`AttributeError` on the wire — or is smuggled as a doc body byte-indistinguishable from real doc
text, which is worse: a caller cannot tell a denial from a document that happens to discuss
denials.

**The observable defect is a transport fault, which the issue does not name.** Measured against
fastmcp 3.4.4: `FastMCP.read_resource` wraps its `_run_middleware` call in no `try`, and
`_read_resource_mcp` catches only `DisabledError` and `NotFoundError`. A bare `AuthorizationError`
therefore matched no handler and escaped to the MCP SDK's
`Server._handle_request`, whose `except Exception` returns `ErrorData(code=0, message=str(err))`.
The caller received the **internal-error** shape, not a denial — and the message was
`"<uri> requires a platform role"`.

**A second defect, also unnamed: the two arms disagreed.** The listing concealed the doc's
existence; the read confirmed it, and named the gate. `resources/read` was an existence oracle for
precisely the docs `on_list_resources` had hidden, which is the property ADR-0097's no-leak
invariant exists to prevent. Any fix that answers "denied" keeps that oracle open.

The precedent is upstream and unambiguous: fastmcp's own component-auth in `_get_resource` returns
`None` on an `AuthorizationError`, commented *"return None if unauthorized (consistent with list
filtering)"*. FastMCP already decided that on this plane, unauthorized reads as absent.

## Decision

**1. The gated read raises `fastmcp.exceptions.NotFoundError`.** `_read_resource_mcp` maps it to
JSON-RPC `-32002` (`"Resource not found: …"`), a documented, non-`code 0` answer that matches what
`on_list_resources` already implies.

**2. The message is fastmcp's own `Unknown resource: {uri!r}`, verbatim.** This is load-bearing,
not cosmetic. `_read_resource_mcp` interpolates the exception into the wire message
(`f"Resource not found: {e}"`), so the raise-site text *is* the disclosure. Matching fastmcp's
never-registered wording byte for byte makes a gated doc and a URI that was never registered
produce an identical code **and** an identical message modulo the URI. A `NotFoundError` carrying
"requires a platform role" would satisfy decision 1 and still leak; the test suite asserts the
equality, not the class, for that reason.

**3. No audit row is written, deliberately.** ADR-0062 §5 audits the **member over-reach**
`RoleDenied` and rejects auditing the routine no-grant case, on the grounds that it lets any
authenticated token amplify writes into `audit_log`. A doc read is openly callable and unauthenticated
callers reach it, so auditing it is exactly that amplification — on a surface with no side effects,
gating signposts for tools that remain independently gated at invocation. This is recorded here
rather than left as silence, because the absence of a row is otherwise indistinguishable from the
bug this ADR fixes.

**4. The resources plane answers in its own vocabulary, and that is now stated.** The tool plane's
contract is `ToolResponse` + `ErrorCategory`; the resources plane's is JSON-RPC error codes. They
are not unified, and unifying them is not deferred work — the transport shapes differ, so there is
nothing to unify.

## Consequences

- A non-platform caller reading an operator doc receives `-32002 Resource not found` instead of a
  `code 0` internal error. Any client branching on code 0 for this case sees a changed code; none
  is known to, and code 0 is not a documented outcome of anything.
- **The read arm no longer reports why.** A platform operator who mistypes their token now gets
  "not found" for a doc that exists. That cost is accepted: the caller cannot distinguish it from a
  typo'd URI, which is the same trade ADR-0097 already made for ungranted rows, and the diagnosis
  path is `session.whoami` (ADR-0471), not the error text.
- **The gated read leaves no server-side trace at all** — no `audit_log` row per decision 3, and
  no log line either. That is deliberate and symmetric: `on_list_resources` does not log the docs
  it drops, so logging the read would reintroduce on the logging plane exactly the asymmetry
  between the two arms that this ADR removes on the wire. The degraded-auth path keeps its
  existing `_log.warning`, because a failing role check is a server fault rather than a
  concealment.
- `tests/guards/test_denial_envelope_actions.py` is **not** extended. It is an AST walk over
  `ErrorCategory.AUTHORIZATION_DENIED` call arguments; this site raises no categorized error and
  builds no envelope, so registering it there is inapplicable — the guard would have nothing to
  match. #1682's proposal to register it rests on the envelope premise refuted above.
- `DenialAuditMiddleware`, `UsageTrackingMiddleware`, `_DENIAL_TYPES`, the `ErrorCategory` taxonomy,
  `ToolResponse`, the tool list, RBAC, and every tool's schema are untouched. No migration, no
  configuration setting.
- **The plane stays uncovered by any boundary, and this ADR does not change that.** It fixes the one
  raiser. A second gated hook added to `doc_exposure.py` — or a new resources-plane middleware —
  would face the same decision with no structural guard forcing the same answer. Nothing in
  `just ci` fails a resources-plane raise of a non-`NotFoundError`. A guard over that shape is the
  durable answer and is deliberately out of scope: it needs to distinguish a concealment raise from
  an ordinary read failure, which is not a grep. Recorded so the next reader knows the coverage is
  one site, not an invariant.

## Alternatives considered

- **Envelope the denial as `ToolResponse.denied` in the resource `contents`** (#1682's proposal).
  Rejected on two independent grounds: it dies at `to_mcp_result` or arrives as an undetectable doc
  body (see Context), and even if representable it would answer "denied", re-opening the existence
  oracle that decision 2 closes.
- **Raise a denial class `DenialAuditMiddleware` owns, or widen `_DENIAL_TYPES` to the base
  `AuthorizationError`.** Rejected: the middleware implements `on_call_tool` only, so neither has
  any effect on `resources/read`. ADR-0486 also chose that closed tuple deliberately, and widening
  it would resurrect `require_platform_role` and `DestructiveOpDenied` denials their own handlers
  already envelope.
- **Keep `AuthorizationError` and add an `except` arm to translate it at the app boundary.**
  Rejected: it adds a translation layer to reach an answer the raise site can state directly, and
  the layer would sit outside `read_resource`, which has no `try` to extend — meaning a new
  wrapper around a library entry point, maintained against a library that already returns `None`
  for this case one frame lower.
- **Raise fastmcp's own `AuthorizationError` (a `FastMCPError`) instead of the repo's.** Rejected:
  it changes nothing observable. The middleware chain runs in `read_resource`'s outer branch, which
  has no `except FastMCPError` — only the inner `run_middleware=False` branch does — so it escapes
  to the SDK catch-all and still reaches the client as `code 0`.
- **Make `on_list_resources` disclose the operator docs instead, so both arms say "denied".**
  Rejected: it inverts the ADR-0284 decision that operator docs are not advertised to callers who
  cannot use their tools, and turns the doc listing into a map of the platform surface for every
  authenticated token.
