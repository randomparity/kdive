# 0709 — An invalid tool-response envelope is a server fault

## Status

Accepted (2026-09-29)

## Context

ADR-0019 makes `ToolResponse` fail fast when a producer builds an invalid envelope: the
category-iff-failure validator raises, and pydantic wraps the raise in a `ValidationError`.
Inside a tool body that exception is misreported. FastMCP 3.4.4 treats any pydantic
`ValidationError` escaping a tool as an argument failure: `FastMCP.call_tool` logs
`Invalid arguments for tool '<name>'` at WARNING and re-raises, so the caller gets `is_error`
with the raw validation text and no envelope (#2932; live in #2916, earlier in #582). The log
happens inside the inner `call_tool`, before any kdive middleware sees the exception, so a
middleware that re-envelopes the error cannot correct the log.

`BindingErrorMiddleware` relies on raw pydantic `ValidationError`s from tool bodies that rebuild
their typed payloads, so pydantic errors from other models must keep their current path.

## Decision

`ToolResponse` validation never lets a pydantic `ValidationError` escape. One wrap-mode model
validator runs the field validation, translates a `ValidationError` into
`InvalidEnvelopeError` (a plain `Exception`, not a `ValueError`), and then checks the
category-iff-failure invariant and that `error_category` is an `ErrorCategory` value, raising
`InvalidEnvelopeError` directly. The validator runs for the constructor, the factory
classmethods, and `model_validate` alike, which covers an idempotency replay rebuilt from a
stored document. `ToolResponse.denied`'s own guard against `missing_roles` in `data` raises
`InvalidEnvelopeError` too.

FastMCP logs any other exception from a tool at ERROR with its traceback (`logger.exception`) and
raises `ToolError` chained from it. `InvalidEnvelopeMiddleware`, registered innermost, turns a
`ToolError` whose cause is `InvalidEnvelopeError` into an `infrastructure_failure` envelope with
the tool name as `object_id` and a fixed `detail` that discloses nothing about the fault
(ADR-0123). Every other exception passes through unchanged.

This amends ADR-0019's fail-fast rule for server faults: construction still refuses an invalid
envelope, and the caller now receives a server-fault envelope instead of an argument error.

## Consequences

- Code that catches an envelope failure on purpose migrates to `InvalidEnvelopeError`: the
  `CompactResponseMiddleware` probe, and the per-row isolation ADR-0019 requires of list tools
  (`artifacts.list`, `allocations.list`), which caught it as a `ValueError`.
- `InvalidEnvelopeError` is not a `ValueError`, so a tool's `except ValueError` for caller input
  cannot absorb an envelope bug and relabel it as the caller's fault.
- `infrastructure_failure` carries `retryable: true`. A retry repeats a data-dependent envelope
  bug, and after an unkeyed mutation that committed before its envelope failed, a retry can
  repeat the side effect; duplicate protection there rests on the site's own dedup key. The
  ERROR log with traceback is the operator's signal.
- Pydantic failures from models other than `ToolResponse` keep FastMCP's argument-error
  handling. Re-enveloping those is out of scope.
- The translation depends on FastMCP 3.4.4's `ToolError`-from-exception wrapping; an in-process
  test through a real FastMCP app pins it, so an upgrade that changes it fails the suite.

### Amendment (2026-09-29): rebuilds of stored data are server faults too (#2981)

This narrows the out-of-scope consequence above. `InvalidEnvelopeError` now subclasses
`ServerFaultError`, which is also not a `ValueError`. `validate_stored(model, value)` in
`kdive.mcp.responses` rebuilds a model from the server's own stored data, such as a database row
or a recorded job payload. When that rebuild fails, it raises `ServerFaultError` chained from
the pydantic error. The middleware is renamed to `ServerFaultMiddleware`, and its constant to
`SERVER_FAULT_DETAIL`, with the same text. It envelopes a `ToolError` caused by any
`ServerFaultError`.

The boundary is an allowlist. Only the tool-body sites that rebuild stored data use the helper.
Those are the reads in `catalog/availability`, `catalog/images`, `catalog/image_visibility`,
`debug/sessions/read`, `external_boot/recovery_idempotency`, `lifecycle/runs/list`,
`lifecycle/systems/admin`, `lifecycle/systems/view`, and `ops/resources/host_ops`.

The following keep their current behaviour:

- Caller-input rebuilds keep FastMCP's argument-error path and `BindingErrorMiddleware`.
- The list tools that isolate a bad row with `except ValueError` still degrade that row.
- Every model build outside the allowlist, including rebuilds in the repository layer, keeps
  FastMCP's argument-error path.

We rejected treating every bare `ValidationError` as a fault (a denylist). judgment: it would
relabel any unmarked caller-input rebuild as a retryable server fault.

## Considered & rejected

- **Middleware alone, keyed on `ValidationError.title == "ToolResponse"`.** verified: in
  `fastmcp/server/server.py` (fastmcp-slim 3.4.4) the `except PydanticValidationError` branch
  logs `Invalid arguments for tool` inside the `run_middleware=False` call, before middleware
  runs, so the misleading log would remain.
- **Override `__init__` to translate.** verified: pydantic's `model_validate` does not call
  `__init__`, and `_idempotency._envelope` rebuilds replay envelopes through it — the #2916 path.
- **Raise the dedicated exception from the existing after-validator only.** verified: a
  prototype under pydantic 2.13 showed field errors (a non-JSON `data` value, a wrong type)
  still escape as `ValidationError`; only a wrap validator sees both.
- **Make `InvalidEnvelopeError` a `ValueError`.** verified: under pydantic 2.13.4 a `ValueError`
  subclass raised by a nested model's validator surfaces from the outer model as a
  `ValidationError`, reopening the misreport; a tool's `except ValueError` could also absorb it.
- **Do nothing; fix each producer as found.** judgment: #582 and #2916 show the class recurs,
  and each occurrence is misreported until someone reads the log closely.
