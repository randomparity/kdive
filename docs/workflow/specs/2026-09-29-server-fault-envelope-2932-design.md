# Invalid envelope is a server fault (#2932) — design

Decision record: [ADR 0709](../../adr/0709-invalid-envelope-is-a-server-fault.md).

## Problem

A tool body that builds or rebuilds an invalid `ToolResponse` lets a pydantic `ValidationError`
escape. FastMCP 3.4.4 logs it as `Invalid arguments for tool '<name>'` (WARNING) and the caller
gets `is_error` with raw validation text and no envelope. The fault is the server's.

## Design

1. `src/kdive/mcp/responses.py` adds `class InvalidEnvelopeError(Exception)`. The existing
   after-validator `_category_iff_failed` becomes a wrap-mode model validator `_validated`:
   - `model = handler(data)`; a `ValidationError` from `handler` is re-raised as
     `InvalidEnvelopeError(str(exc))` chained from it;
   - the category-iff-failure checks then raise `InvalidEnvelopeError` with the existing
     messages (`status 'x' requires an error_category`,
     `error_category set on non-failure status 'x'`);
   - an `error_category` that is not an `ErrorCategory` value raises
     `InvalidEnvelopeError("unknown error_category 'x'")`;
   - `retryable` is derived as today.
   The validator runs for `ToolResponse(...)`, every factory, and `model_validate`.
   `ToolResponse.denied`'s `missing_roles`-in-`data` guard raises `InvalidEnvelopeError`.
2. `src/kdive/mcp/middleware/invalid_envelope.py` adds `InvalidEnvelopeMiddleware.on_call_tool`:
   on `ToolError` whose `__cause__` is an `InvalidEnvelopeError`, return
   `ToolResult(structured_content=ToolResponse.failure(<tool name>, INFRASTRUCTURE_FAILURE,
   detail=INVALID_ENVELOPE_DETAIL).model_dump(mode="json"))`; re-raise anything else.
   `INVALID_ENVELOPE_DETAIL = "the server could not build this tool's response"`.
3. `src/kdive/mcp/assembly/app.py` registers it last (innermost), after
   `BindingErrorMiddleware`, so every other middleware observes an ordinary failure envelope.
4. Callers that catch an envelope failure on purpose migrate:
   `src/kdive/mcp/middleware/compact.py` catches `InvalidEnvelopeError` instead of
   `ValidationError`; the ADR-0019 per-row isolators in
   `src/kdive/mcp/tools/catalog/artifacts/reads.py` (`_artifact_list_items`) and
   `src/kdive/mcp/tools/lifecycle/allocations/view.py` (the `allocations.list` row loop) catch
   `(ValueError, InvalidEnvelopeError)`. An AST scan of `src/` for `try` bodies building a
   `ToolResponse` under `except ValueError|ValidationError|Exception` found no other site.
5. `src/kdive/mcp/resources/_content/response-envelope.md` gains one sentence: an envelope the
   server fails to build is reported as `infrastructure_failure` with a fixed detail.

The FastMCP ERROR log (`logger.exception("Error calling tool ...")`) is the server-side record;
kdive adds no second log line.

## Failure model

1. **Actors and deployments** — authenticated MCP callers of any registered tool; the MCP server
   process (compose, Helm, local stack) on fastmcp-slim 3.4.4.
2. **Invariants and assets at stake** — the public MCP error contract (a server fault must not
   read as a caller argument error); ADR-0123 no-leak `detail`; `BindingErrorMiddleware`'s four
   conversions of raw pydantic errors from non-envelope models.
3. **Accepted failure classes** — `retryable: true` on the server-fault envelope (the
   category's fixed ADR-0118 value; a retry of a data-dependent bug repeats it, bounded by the
   ERROR log; after an unkeyed mutation that committed before its envelope failed, a retry can
   repeat the side effect, bounded by the site's dedup key); an invalid `ToolResponse` built outside a tool call (worker, CLI) raises
   `InvalidEnvelopeError` unenveloped, as it raised `ValidationError` before.
4. **Covered elsewhere** — pydantic errors from non-`ToolResponse` models (follow-up issue per
   the approved exclusions); FastMCP's own classification of pydantic errors (upstream);
   binding conversions for other tools (#2304/#2325 line).

## Success

- A tool body whose `ToolResponse` fails validation (constructor, factory, `denied` guard, or
  `model_validate`) returns an
  `infrastructure_failure` envelope, `detail == INVALID_ENVELOPE_DETAIL`, not `is_error`.
- For that call FastMCP emits one ERROR record with `exc_info` and no
  `Invalid arguments for tool` record.
- A typed-argument error on a tool outside `_BINDING_CONVERSIONS` is still `is_error` with the
  `Invalid arguments for tool` WARNING; the `BindingErrorMiddleware` tests stay green.

## Validation

- `tests/mcp/core/test_responses.py`: invariant, field-type, unknown-category, nested-item,
  `model_validate`, and `denied` guard failures raise `InvalidEnvelopeError`; existing
  `pytest.raises(ValueError, ...)` cases migrate.
- `tests/mcp/catalog/test_artifact_list_isolation.py` and
  `tests/mcp/lifecycle/test_allocation_list_isolation.py`: one row whose envelope raises
  `InvalidEnvelopeError` is isolated and the other rows are returned.
- `tests/mcp/middleware/test_invalid_envelope.py`: in-process FastMCP app with both middlewares
  and a real `fastmcp.Client`; asserts the envelope, the log records (caplog), and the argument
  error path.
- `tests/mcp/core/test_app.py`: `InvalidEnvelopeMiddleware` is the last registered middleware.
- `tests/mcp/middleware/test_compact.py`: a non-envelope dict with envelope-subset keys passes
  through unchanged (existing coverage; must stay green).
