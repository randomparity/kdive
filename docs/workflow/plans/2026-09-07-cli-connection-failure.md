# kdivectl Connection-Failure Diagnostic Implementation Plan

Goal: make supported MCP endpoint connection failures concise, actionable, nonzero, and
secret-safe without changing authentication, ToolError, or ToolResponse behavior.

Architecture: `Session` continues to own URL/token resolution and FastMCP client construction.
The shared `dispatch.run` exception boundary classifies the installed client's connection-failure
shape and emits a constant diagnostic, while all non-connection paths retain their current flow.

Tech stack: Python 3.14, FastMCP 3.4.4, HTTPX 0.28.1, pytest.

Expected implementation size: 60–100 changed lines (S) — derived from two classifiers, two dispatch
branches, and focused CLI transport tests.

## Global Constraints

- Support Python 3.14 on x86_64 and ppc64le.
- Use the already pinned FastMCP 3.4.4 and HTTPX 0.28.1 dependencies; add no dependency.
- Never render exception text, a configured URL, a request, headers, or bearer-token content for a
  connection failure.
- Preserve ToolError handling and ToolResponse exit maps; give HTTP 401/403 a distinct constant
  authentication diagnostic while leaving other HTTP statuses unchanged.
- Do not implement retries/timeouts, server-side operation changes, credential/config-file
  discovery, or production OIDC login.
- Keep Python lines at or below 100 characters and run repository `just` guardrails.

## Task 1: Classify and render connection failures at the shared boundary

Files:

- Modify `src/kdive/cli/transport.py` to define
  `is_connection_failure(exc: BaseException) -> bool` and
  `is_authentication_failure(exc: BaseException) -> bool`.
- Modify `src/kdive/cli/dispatch.py` to use the predicate after its existing `ToolError` handler.
- Modify `tests/cli/test_transport.py` and `tests/cli/test_tool_error_handling.py` for focused proof.

Interfaces:

- Consumes FastMCP 3.4.4's verified `RuntimeError`-from-`httpx.ConnectError` client-entry shape and
  HTTPX 0.28.1's `ConnectError` and `HTTPStatusError` types.
- Provides `is_connection_failure(exc: BaseException) -> bool` and
  `is_authentication_failure(exc: BaseException) -> bool` to `dispatch.run`.
- Preserves `dispatch.run(args: argparse.Namespace) -> int` for every CLI caller.

Verification:

- Contract: classify only supported connection-failure shapes. Mode: focused-test. Add cases in
  `tests/cli/test_transport.py` that first fail because the predicate is absent, then prove direct
  and immediate-cause-wrapped `ConnectError` are true while unrelated, deeper, and cyclic chains
  are false. Add authentication cases proving only HTTP 401/403 match the second predicate. Green
  command:
  `uv run python -m pytest tests/cli/test_transport.py -q` with all tests passing.
- Contract: CLI stderr/exit and secret safety. Mode: focused-test. Add a real parser-to-dispatch
  case in `tests/cli/test_tool_error_handling.py` that configures the real `Session.client` and
  FastMCP client against a deterministically closed local port; before implementation it propagates
  `RuntimeError`, and after implementation it returns 1, prints the fixed
  `KDIVE_SERVER_URL`/server-availability advice, omits traceback, and omits seeded token,
  credentials, query value, and underlying message. Green command:
  `uv run python -m pytest tests/cli/test_tool_error_handling.py -q` with all tests passing.
- Contract: authentication distinction and secret safety. Mode: focused-test. Add a local one-shot
  HTTP server returning 401 and invoke the real `kdivectl` process with synthetic URL credentials
  and a sensitive query value. Before implementation it emits a traceback containing those URL
  values; after implementation it returns 1 with the fixed authentication diagnostic and neither
  output stream contains the traceback, credentials, query value, or bearer token. Green command:
  `uv run python -m pytest tests/cli/test_tool_error_handling.py -q` with all tests passing.
- Contract: auth, ToolError, no-token, and ToolResponse compatibility. Mode: focused-test. The
  existing tests in `tests/cli/test_transport.py` and `tests/cli/test_tool_error_handling.py` must
  remain green under the two commands above.

Steps:

1. Add the classifier tests and CLI boundary regression test, then run their exact focused commands
   and record the expected missing predicate/uncaught wrapped exception failures.
2. Implement `is_connection_failure` by checking only the exception and its immediate `__cause__`
   for `httpx.ConnectError`; implement `is_authentication_failure` by checking a direct
   `httpx.HTTPStatusError` response for status 401 or 403. Do not inspect or format exception text.
3. Extend `dispatch.run` with a later generic exception arm that re-raises nonmatches, prints
   `error: connection failed; verify KDIVE_SERVER_URL and server availability` to stderr and
   returns 1 for connection matches, and prints a separate fixed authentication diagnostic and
   returns 1 for authentication matches.
4. Run both focused commands and expect all tests to pass.
5. Run `just format`, `just lint`, `just type`, and `just test-changed`; expect exit 0 from each.
6. Stage only the four implementation/test paths, run `prek run`, re-add only those paths if hooks
   rewrite them, and commit with a Conventional Commits subject.

Acceptance:

- Each success criterion in the design has a focused assertion.
- No non-connection exception is converted into the endpoint diagnostic.
- The diff contains no new dependency, retry, timeout, credential, or configuration-file behavior.

Rollback: revert the implementation commit; no data, schema, deployment ordering, or external
cleanup is involved.
