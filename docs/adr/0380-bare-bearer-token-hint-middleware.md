# ADR 0380 — Bare-token Authorization hint middleware

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-17
- **Deciders:** kdive maintainers

## Context

When an MCP client sends `Authorization` as a bare JWT **without** the `Bearer `
scheme prefix, the server rejects it with a message asserting the token is
"invalid, expired, or no longer recognized" — even when the token is valid and
unexpired (#1268, `BLACK_BOX_REVIEW.md` F-01). The real cause (missing scheme
prefix) is never surfaced, sending the client to decode/verify the token and probe
the transport before a human identifies the trivial fix.

Both the misleading message and the strict prefix requirement live in **vendored
dependencies**, not kdive:

- MCP SDK `BearerAuthBackend.authenticate` short-circuits to `None` on any header
  that does not `lower().startswith("bearer ")`, **before** verifying the token —
  so validity/expiry is never checked for a bare token.
- FastMCP `RequireAuthMiddleware._send_auth_error` hard-codes the "invalid,
  expired, or no longer recognized" description for *every* 401 `invalid_token`,
  regardless of whether a token was even parsed.

kdive owns no `Authorization`-parsing seam today (`src/kdive/mcp/auth.py` only
constructs FastMCP's `JWTVerifier`). Requiring the `Bearer ` prefix is
RFC 6750-correct, so this is a diagnosability defect, not an auth bypass — the fix
must not weaken the prefix requirement (auto-accepting bare JWTs would deviate from
the OAuth2 Bearer spec).

`RequireAuthMiddleware` is not a Starlette middleware in FastMCP's stack — it is the
**endpoint wrapper** on the streamable-HTTP route. Any Starlette/ASGI middleware
passed through `FastMCP.run_async(transport="http", middleware=[...])` therefore runs
*ahead* of it and can intercept the request before the misleading error is produced.

## Decision

We will add a kdive-owned ASGI middleware, `BareBearerHintMiddleware`, injected into
the FastMCP HTTP app's Starlette `middleware=` list (via
`kdive.mcp.assembly.http_middleware.http_asgi_middleware`, wired at
`processes/server.py`). It inspects the raw `Authorization` header and, when the value
looks like a **bare JWT** — no whitespace, starts with `eyJ`, three dot-separated
segments, and no `bearer ` scheme prefix — short-circuits with a specific, accurate
`401` telling the client to prefix the token with `Bearer `. Every other value
(scheme-prefixed, non-JWT, or absent) passes through untouched to the vendored auth
path. We do not patch vendored code.

The short-circuit response uses OAuth error code `invalid_request` (the header is
malformed, not a token that failed verification) at HTTP `401` (so the client's
auth-retry path still triggers), with a matching `WWW-Authenticate: Bearer` header,
mirroring the vendored JSON error shape.

## Consequences

- A first-time client with a valid token gets an actionable message pointing at the
  missing scheme prefix instead of a wild-goose chase through token verification.
- kdive now owns a request-path seam ahead of vendored auth. It is deliberately
  narrow (a single, precise bare-JWT heuristic) so it never shadows a genuine auth
  failure: a scheme-prefixed token — valid or invalid — always reaches the vendored
  verifier and its normal error.
- The heuristic is intentionally conservative (`eyJ` + exactly two dots + no
  whitespace). A value that is not obviously a JWT is passed through rather than
  risk a false-positive hint on some other malformed header.
- New obligation: if FastMCP changes how `middleware=` is threaded relative to the
  auth endpoint wrapper, the ordering test must catch it — an integration test drives
  the real `http_app` to prove a bare token gets our hint while an invalid `Bearer`
  token still gets the vendored error.
- No migration, no new dependency, no RBAC or schema change.

## Alternatives considered

- **Documentation only** (state the `Bearer ` prefix requirement in the MCP
  connection-setup docs). Rejected as the primary fix: the server *actively
  misdirects* with a false "token invalid/expired" claim, so a doc note a client
  never reads does not stop the wasted investigation. (Docs are still updated as a
  complement, not the fix.)
- **Auto-accept bare JWTs as Bearer.** Rejected: deviates from the RFC 6750 Bearer
  scheme; silently accepting unschemed credentials is a security-posture change, not
  a diagnosability fix.
- **Subclass/patch the vendored `RequireAuthMiddleware` or `BearerAuthBackend`.**
  Rejected: patching vendored code is fragile across upgrades and couples kdive to
  private internals; an ASGI middleware ahead of the endpoint wrapper is a supported
  extension seam.
- **OAuth code `invalid_token` at 401** (byte-match the vendored contract). Rejected
  in favor of `invalid_request`: the header is malformed (no scheme), not a token
  that failed verification, and `invalid_request` is the RFC 6750-correct code for a
  malformed request while 401 preserves the client's auth-retry behavior.
