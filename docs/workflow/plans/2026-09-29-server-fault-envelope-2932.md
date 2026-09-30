# Invalid envelope is a server fault (#2932) — plan

Goal: an invalid `ToolResponse` built inside a tool reaches the caller as an
`infrastructure_failure` envelope and the log as an ERROR with traceback.

Architecture: `ToolResponse` translates every validation failure into `InvalidEnvelopeError`
(wrap-mode model validator); FastMCP logs that non-pydantic exception at ERROR and wraps it in
`ToolError`; an innermost `InvalidEnvelopeMiddleware` returns the server-fault envelope. Spec:
`docs/workflow/specs/2026-09-29-server-fault-envelope-2932-design.md`; ADR 0709.

Tech stack: Python 3.14, pydantic 2.13.4, fastmcp-slim 3.4.4, pytest.

Expected implementation size: 150–220 changed lines (M) — two source edits, one new middleware
module, one registration line, one doc sentence, and the tests in Tasks 1–2.

## Global Constraints

- No new dependency. FastMCP stays pinned at `fastmcp-slim[server,client]==3.4.4`.
- `detail` on the server-fault envelope is the fixed constant; no exception text reaches it
  (ADR-0123).
- Guardrails: `just lint`, `just type`, `just test-verbose <paths>`; pre-push `just ci`.
- Wording rules from AGENTS.md apply to docstrings and docs.

## File map

| File | Change | Owns after |
|---|---|---|
| `src/kdive/mcp/responses.py` | modify | `InvalidEnvelopeError`; wrap validator replacing `_category_iff_failed` |
| `src/kdive/mcp/middleware/invalid_envelope.py` | create | `InvalidEnvelopeMiddleware`, `INVALID_ENVELOPE_DETAIL` |
| `src/kdive/mcp/assembly/app.py` | modify | registers the middleware last |
| `src/kdive/mcp/middleware/compact.py` | modify | probe catches `InvalidEnvelopeError` (caller migration) |
| `src/kdive/mcp/resources/_content/response-envelope.md` | modify | one sentence on the server-fault envelope |
| `tests/mcp/core/test_responses.py` | modify | envelope-validation tests |
| `tests/mcp/middleware/test_invalid_envelope.py` | create | end-to-end middleware + log tests |
| `tests/mcp/core/test_app.py` | modify | innermost-registration test |

No obsolete path remains: the after-validator is replaced, not kept beside the wrap validator.

## Task 1 — `ToolResponse` raises `InvalidEnvelopeError`

Interfaces: produces `kdive.mcp.responses.InvalidEnvelopeError(Exception)`; consumed by Task 2
and `compact.py`.

Verification:
- Contract: invariant, field, nested-item and `model_validate` failures raise
  `InvalidEnvelopeError`, never `ValidationError`. Mode: focused-test —
  `tests/mcp/core/test_responses.py::test_invalid_envelope_*`; red: `ImportError` for
  `InvalidEnvelopeError`; green: `just test-verbose tests/mcp/core/test_responses.py`.
- Contract: compact probe still passes a non-envelope through. Mode: focused-test —
  `tests/mcp/middleware/test_compact.py::test_enabled_passes_item_with_extra_key_through_unchanged`
  (red after the responses change and before the compact change: `InvalidEnvelopeError`
  escapes); green: `just test-verbose tests/mcp/middleware/test_compact.py`.

Steps:
1. In `test_responses.py`, change the four `pytest.raises(ValueError, match=...)` cases for
   `error_category` and `non-JSON` to `pytest.raises(InvalidEnvelopeError, match=...)`, and add:
   ```python
   def test_invalid_envelope_from_model_validate() -> None:
       with pytest.raises(InvalidEnvelopeError, match="requires an error_category"):
           ToolResponse.model_validate({"object_id": "x", "status": "failed"})


   def test_invalid_envelope_from_wrong_field_type() -> None:
       with pytest.raises(InvalidEnvelopeError) as info:
           ToolResponse.model_validate({"object_id": 1, "status": "ok"})
       assert isinstance(info.value.__cause__, ValidationError)


   def test_invalid_envelope_from_nested_item() -> None:
       with pytest.raises(InvalidEnvelopeError, match="non-failure status"):
           ToolResponse.model_validate(
               {
                   "object_id": "c",
                   "status": "ok",
                   "items": [{"object_id": "i", "status": "ok", "error_category": "not_found"}],
               }
           )


   def test_invalid_envelope_is_not_a_value_error() -> None:
       assert not issubclass(InvalidEnvelopeError, ValueError)
   ```
   Run; expect ImportError.
2. In `responses.py` add after the imports:
   ```python
   class InvalidEnvelopeError(Exception):
       """A producer built an invalid ``ToolResponse`` — a server fault (ADR-0709)."""
   ```
   Replace `_category_iff_failed` with:
   ```python
   @model_validator(mode="wrap")
   @classmethod
   def _validated(cls, data: object, handler: ModelWrapValidatorHandler[ToolResponse]) -> ToolResponse:
       try:
           model = handler(data)
       except ValidationError as exc:
           raise InvalidEnvelopeError(str(exc)) from exc
       is_failure = model.status in _FAILURE_STATUSES
       if is_failure and model.error_category is None:
           raise InvalidEnvelopeError(f"status {model.status!r} requires an error_category")
       if not is_failure and model.error_category is not None:
           raise InvalidEnvelopeError(f"error_category set on non-failure status {model.status!r}")
       model.retryable = (
           retryable_category(ErrorCategory(model.error_category)) if is_failure else None
       )
       return model
   ```
   keeping the existing docstring, amended to cite ADR-0709. Import `ModelWrapValidatorHandler`
   and `ValidationError` from `pydantic`. Update the `success()` docstring ("the model validator
   raises `InvalidEnvelopeError`").
3. Run the focused tests; expect green. Run the compact tests; expect the extra-key case red.
4. In `compact.py` replace `from pydantic import ValidationError` and the `except ValidationError`
   with `InvalidEnvelopeError` imported from `kdive.mcp.responses`. Compact tests green.
5. Commit: `fix(mcp): raise InvalidEnvelopeError for an invalid ToolResponse`.

## Task 2 — server-fault envelope through the middleware

Interfaces: consumes `InvalidEnvelopeError`; produces
`kdive.mcp.middleware.invalid_envelope.InvalidEnvelopeMiddleware` and `INVALID_ENVELOPE_DETAIL`.

Verification:
- Contract: an invalid envelope in a tool body returns the `infrastructure_failure` envelope with
  the fixed detail; one ERROR record with `exc_info`; no `Invalid arguments for tool` record.
  Mode: focused-test — `tests/mcp/middleware/test_invalid_envelope.py`; red: module import
  error; green: `just test-verbose tests/mcp/middleware/test_invalid_envelope.py`.
- Contract: a typed-argument error still reports as one (`is_error`, WARNING
  `Invalid arguments for tool`). Mode: focused-test — same file.
- Contract: middleware is registered innermost. Mode: focused-test —
  `tests/mcp/core/test_app.py::test_invalid_envelope_middleware_is_registered_innermost`; red:
  assertion on `app.middleware[-1]`; green: `just test-verbose tests/mcp/core/test_app.py`.
- Contract: served doc sentence. Mode: task-test-not-applicable — prose in a served resource;
  the existing served-doc equality test covers delivery, and no consumer parses the sentence.

Steps:
1. Write `tests/mcp/middleware/test_invalid_envelope.py`: build `FastMCP("t")`, add
   `BindingErrorMiddleware()` then `InvalidEnvelopeMiddleware()`; register tool `bad` returning
   `ToolResponse(object_id="x", status="queued", error_category="not_found")`, tool `replay`
   returning `ToolResponse.model_validate({"object_id": "x", "status": "failed"})`, and tool
   `typed(n: int)` returning `ToolResponse.success("t", "ok")`. Via
   `fastmcp.Client(app)` call each with `raise_on_error=False`, capture logs with `caplog` at
   DEBUG (attach to the `fastmcp` logger if it does not propagate). Assert for `bad` and
   `replay`: not `is_error`; `structured_content["error_category"] == "infrastructure_failure"`,
   `["detail"] == INVALID_ENVELOPE_DETAIL`, `["object_id"]` is the tool name; an ERROR record
   with `exc_info`; no record containing `Invalid arguments for tool`. For `typed` with
   `{"n": "abc"}`: `is_error` and a WARNING record containing `Invalid arguments for tool`.
2. Create `src/kdive/mcp/middleware/invalid_envelope.py`:
   ```python
   INVALID_ENVELOPE_DETAIL = "the server could not build this tool's response"


   class InvalidEnvelopeMiddleware(Middleware):
       async def on_call_tool(self, context: Any, call_next: Callable[[Any], Any]) -> Any:
           try:
               return await call_next(context)
           except ToolError as exc:
               if not isinstance(exc.__cause__, InvalidEnvelopeError):
                   raise
           envelope = ToolResponse.failure(
               str(context.message.name),
               ErrorCategory.INFRASTRUCTURE_FAILURE,
               detail=INVALID_ENVELOPE_DETAIL,
           )
           return ToolResult(structured_content=envelope.model_dump(mode="json"))
   ```
   with a module docstring citing ADR-0709 and why FastMCP's own ERROR log is the record.
3. In `app.py`, import it and add `app.add_middleware(InvalidEnvelopeMiddleware())` after
   `BindingErrorMiddleware()`. Add the `test_app.py` test asserting
   `type(app.middleware[-1]) is InvalidEnvelopeMiddleware`.
4. Add to `response-envelope.md` under "The `error_category` invariant": "If the server fails to
   build a valid envelope, the call returns `infrastructure_failure` with a fixed `detail`; it
   is a server fault, not an argument error."
5. `just lint`, `just type`, focused tests green. Commit:
   `fix(mcp): envelope an invalid ToolResponse as infrastructure_failure`.

## Rollback

Revert the two commits; no persisted state or migration is involved.
