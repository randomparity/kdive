# Server-built model validation failures are server faults (#2981)

## Problem

ADR-0709 (#2932) reports an invalid `ToolResponse` built in a tool body as a server fault. A
pydantic `ValidationError` from any other model a tool body builds still escapes to FastMCP
3.4.4, which logs `Invalid arguments for tool '<name>'` at WARNING before any middleware runs
and returns `is_error` with the raw validation text. When the model is rebuilt from the server's
own stored data (a database row, a recorded job payload), the caller is told its arguments were
wrong and the operator sees a WARNING, not a server fault. Because FastMCP logs before middleware,
the fix must act where the model is built (ADR-0709, Considered & rejected).

## Scope

Operator-approved design (option a): translate at an allowlist of verified server-built sites.

- `ServerFaultError(Exception)` in `src/kdive/mcp/responses.py` becomes the base of the ADR-0709
  family; `InvalidEnvelopeError` subclasses it (its behaviour is unchanged). Not a `ValueError`,
  for the reasons ADR-0709 gives.
- `validate_stored(model, value)` in the same module calls `model.model_validate(value)` and
  re-raises a `ValidationError` as `ServerFaultError("stored <Model> failed validation")`
  chained from it. It lives beside the exception family every tool already imports.
- The middleware module `src/kdive/mcp/middleware/invalid_envelope.py` is renamed to
  `server_fault.py`; `InvalidEnvelopeMiddleware` becomes `ServerFaultMiddleware` and
  `INVALID_ENVELOPE_DETAIL` becomes `SERVER_FAULT_DETAIL` (same text). It envelopes a `ToolError`
  whose `__cause__` is any `ServerFaultError`. The old names are removed; they are internal.
- Allowlist — each value is read from the database or a recorded job payload, never from the
  call's arguments, and no enclosing `except ValueError` relies on the current exception:

  | File under `src/kdive/mcp/tools/` | Model(s) | Source |
  |---|---|---|
  | `catalog/availability.py` `_fetch_resources` | `Resource` | `resources` rows |
  | `catalog/images.py` `list_images` | `ImageCatalogEntry` | catalog rows |
  | `catalog/image_visibility.py` `fetch_visible_image` | `ImageCatalogEntry` | catalog row |
  | `debug/sessions/read.py` `_split_system_id` | `DebugSession` | session rows |
  | `external_boot/recovery_idempotency.py` (2 sites) | `RecoveryRequestV1` | stored job payload |
  | `lifecycle/runs/list.py` `list_runs` | `Run` | `runs` rows |
  | `lifecycle/systems/admin.py` `_job_for_dedup_key` | `Job` | `jobs` row |
  | `lifecycle/systems/view.py` `_split_placement` | `System` | `systems` rows |
  | `ops/resources/host_ops.py` `_apply_cordon`, `_live_allocations` | `Resource`, `Allocation` | rows |

- Left untouched: `catalog/shapes.py` (validates caller request fields — caller input);
  `catalog/resources.py` and `lifecycle/allocations/view.py` (already isolate a bad row per row
  with `except ValueError` and degrade it; translating would break that isolation);
  `lifecycle/support/_idempotency.py` (`ToolResponse`, already ADR-0709). `BindingErrorMiddleware`
  and its caller-input conversions are unchanged.
- ADR-0709 gains a dated amendment recording this boundary. No new ADR, no migration.

## Failure model

1. Actors and deployments: an authenticated MCP caller invoking a read or admin tool; the
   operator reading the server log. The kdive MCP server under FastMCP 3.4.4.
2. Invariants and assets: the public error contract — a caller-input error stays a caller error
   (`BindingErrorMiddleware` result or FastMCP argument error); a server fault never discloses
   stored values to the caller (fixed `detail`, ADR-0123).
3. Accepted failure classes:
   - Model builds in tool bodies outside the allowlist, and repository-layer (`kdive.db`)
     rebuilds a tool calls, keep FastMCP's argument-error path — the operator chose an
     allowlist; a denylist risks relabelling an unmarked caller-input rebuild as a retryable
     server fault.
   - `infrastructure_failure` is `retryable: true` though a bad stored row repeats; ADR-0709
     already accepts this for the envelope, and the ERROR log with traceback is the signal.
   - The traceback in the server log carries the validation text (stored values); it is
     operator-side, as for the envelope, and no longer reaches the caller.
4. Covered elsewhere: FastMCP upgrade or patch (excluded, dependency maintenance); the
   `ToolError`-from-exception wrapping is pinned by the in-process test ADR-0709 relies on.

## Success

1. A tool body whose `validate_stored` call fails returns `status: error`,
   `error_category: infrastructure_failure`, `detail == SERVER_FAULT_DETAIL`, with exactly one
   FastMCP ERROR record carrying `exc_info` and no `Invalid arguments for tool` record.
2. Every allowlisted site in the table calls `validate_stored`; no other site changes.
3. A caller-input rebuild in a tool registered in `BindingErrorMiddleware` still returns its
   `configuration_error` envelope, and a typed-argument binding error still logs the WARNING.
4. `validate_stored` returns the model on valid input and raises `ServerFaultError` (not a
   `ValueError`) chained from the `ValidationError` on invalid input.

## Validation

- Success 1, 3: in-process FastMCP tests in `tests/mcp/middleware/test_server_fault.py`
  (renamed from `test_invalid_envelope.py`).
- Success 4: unit tests in `tests/mcp/core/test_responses.py`.
- Success 2: review of the diff against the table; the per-site rebuilds are DB-backed and the
  helper carries the behaviour.
- Controlled fault: revert the middleware's `ServerFaultError` check to `InvalidEnvelopeError`
  and observe Success 1 go red.
