# kdivectl Connection-Failure Diagnostic Design

Issue: [#2316](https://github.com/randomparity/kdive/issues/2316)

## Problem

FastMCP 3.4.4 wraps an `httpx.ConnectError` raised while entering its client context in a
`RuntimeError` whose cause is the original connection error. `kdivectl` currently handles only
`ToolError` at its shared dispatch boundary, so an unreachable configured MCP endpoint escapes as a
traceback. That output is noisy and can include the configured URL, including credentials or query
values. Missing credentials already fail before the connection attempt with the existing login or
`KDIVE_TOKEN` instruction.

## Scope and constraints

The shared `dispatch.run` boundary will recognize only a direct `httpx.ConnectError` or one as the
immediate cause of FastMCP's `RuntimeError`. It will write a fixed diagnostic to stderr, return
generic exit code 1, and include neither the exception text nor the configured URL. The diagnostic
will name a connection failure and advise checking `KDIVE_SERVER_URL` and server availability.

An HTTP 401 or 403 raised while establishing the session will take a separate fixed authentication
diagnostic that advises checking `KDIVE_TOKEN` and server authorization. It also returns 1 without
formatting the exception or request URL. Other exceptions retain their current behavior. In
particular, `ToolError` remains a separate one-line generic failure, and returned `ToolResponse`
envelopes keep their existing category-to-exit mapping. This change adds no retry, timeout,
server-side operation behavior, credential discovery, configuration-file discovery, or production
OIDC login behavior.

The implementation supports Python 3.14 on x86_64 and ppc64le and uses the already pinned
FastMCP 3.4.4 and HTTPX 0.28.1 dependencies. No dependency or persisted-data change is required.

## Behavior and error flow

Every online CLI path already converges on `dispatch.run`. After preserving its first `ToolError`
handler, that function will inspect otherwise escaping exceptions with small transport predicates.
Connection classification examines only the exception and its immediate cause, matching the two
verified installed-client shapes. Authentication classification matches only direct
`httpx.HTTPStatusError` values whose response status is 401 or 403. Either match emits its own fixed
stderr line and returns 1; a non-match is re-raised unchanged.

`Session.from_env` stays responsible for resolving the endpoint and bearer token. Its fail-closed
no-token path remains earlier than client construction, so it continues to emit its current
credential instruction without being relabeled as a connection failure.

## Threat model

- Boundary inventory: operator-controlled `KDIVE_SERVER_URL` enters FastMCP/HTTPX at the existing
  CLI-to-server boundary. No boundary is added or widened; only failure rendering changes.
- Actors and trust: a local operator controls CLI settings, while endpoint responses and transport
  failures are untrusted. The diagnostic must not trust exception text as safe output.
- Controls: classification uses bounded exception type, immediate-cause, and response-status checks,
  while rendering uses constant strings. The URL, request, headers, bearer token, exception message,
  credentials, and query values are never interpolated. Existing server authorization envelopes and
  ToolResponse handling remain untouched.
- Out of scope: malicious server responses, token acquisition, transport retries/timeouts, and
  server-side operation failures remain owned by their existing boundaries and exclusions.

## Success criteria

1. A refused connection through the supported FastMCP client produces one concise stderr
   diagnostic naming connection failure and recommending verification of `KDIVE_SERVER_URL` and
   server availability, returns 1, and produces no traceback.
2. The diagnostic contains no bearer token, URL credentials, query values, or underlying exception
   text.
3. A missing token still exits before any connection attempt with the existing login/token advice.
4. HTTP 401/403 failures are not classified as connection failures and receive a distinct,
   secret-safe authentication diagnostic; other HTTP status failures retain their current behavior.
5. Existing `ToolError` handling and ToolResponse exit-code mappings remain unchanged.

## Validation

- Add focused transport tests for direct and immediate FastMCP-wrapped `httpx.ConnectError`, deeper
  and cyclic cause chains, a non-connection `RuntimeError`, and HTTP authentication/status failures.
- Add a CLI boundary test that calls the real parser/dispatch path with a connection-failing session
  and asserts stderr, exit 1, no traceback, and absence of seeded token/URL secrets.
- Add a real local HTTP 401 process-boundary test seeded with synthetic URL credentials and a query
  value; assert a distinct fixed diagnostic and no traceback or seeded secret on either stream.
- Retain and run the existing no-token, `ToolError`, authorization-envelope, and exit-mapping tests.
- Run `just lint`, `just type`, `just test-changed`, and the pre-push `just ci` recipe.
