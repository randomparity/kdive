# Plan: server-built model validation failures are server faults (#2981)

Goal: rebuilds of stored data in allowlisted MCP tool bodies report a validation failure as an
ADR-0709 server fault. Spec: `docs/workflow/specs/2026-09-29-server-built-model-fault-2981-design.md`.

Architecture: a `ServerFaultError` base in `kdive.mcp.responses` with a `validate_stored`
helper; the innermost middleware (renamed `ServerFaultMiddleware`) envelopes any `ToolError`
caused by a `ServerFaultError`; allowlisted sites call the helper.

Tech stack: Python 3.14, pydantic 2.x, FastMCP 3.4.4 (`fastmcp-slim`), pytest.

Expected implementation size: 120–180 changed lines (M) — derived from the file map: ~25 lines
in `responses.py`, a ~10-line middleware rename, 11 one-line site edits plus imports, ~75 lines
of tests, a ~15-line ADR amendment.

## Global Constraints

- No new dependency, no migration, no new ADR; amend ADR-0709 only.
- `ServerFaultError` must not subclass `ValueError` (ADR-0709).
- `SERVER_FAULT_DETAIL` keeps the text `"the server could not build this tool's response"`.
- Guardrails: `just lint`, `just type`, `just test-verbose <paths>`, `just records`,
  `just docs-check`; pre-push `just ci`.

## File map

| File | Change |
|---|---|
| `src/kdive/mcp/responses.py` | add `ServerFaultError`, `validate_stored`; `InvalidEnvelopeError(ServerFaultError)` |
| `src/kdive/mcp/middleware/invalid_envelope.py` → `server_fault.py` | rename; catch `ServerFaultError` |
| `src/kdive/mcp/assembly/app.py` | import/register `ServerFaultMiddleware` |
| the nine allowlisted tool modules (spec table) | `X.model_validate(v)` → `validate_stored(X, v)` |
| `tests/mcp/middleware/test_invalid_envelope.py` → `test_server_fault.py` | rename; add stored-rebuild and caller-input cases |
| `tests/mcp/core/test_responses.py` | `validate_stored` unit tests |
| `tests/mcp/core/test_app.py` | renamed middleware in the innermost assertion |
| `docs/adr/0709-invalid-envelope-is-a-server-fault.md` | dated amendment |

## Task 1: server-fault family, helper, and middleware

Interfaces (produced): `class ServerFaultError(Exception)`;
`validate_stored(model: type[M], value: object) -> M` (generic over `M: BaseModel`);
`class ServerFaultMiddleware(Middleware)`; `SERVER_FAULT_DETAIL: str`.

Verification:
- `validate_stored` contract — Mode: focused-test. `tests/mcp/core/test_responses.py`
  `test_validate_stored_returns_model`, `test_validate_stored_failure_is_a_server_fault`; red:
  `ImportError` on `validate_stored`; green: `just test-verbose tests/mcp/core/test_responses.py`.
- Stored-rebuild failure enveloped — Mode: focused-test. `tests/mcp/middleware/test_server_fault.py`
  parametrized `test_server_fault_returns_a_server_fault_envelope` including tool `stored`; red:
  `is_error` true with the WARNING record; green: `just test-verbose tests/mcp/middleware/test_server_fault.py`.
- Caller-input rebuild unchanged — Mode: focused-test. same file,
  `test_caller_input_rebuild_keeps_binding_envelope` (tool `systems.provision`, argument
  `allocation_id`, whose body runs a bare `model_validate` of a model with an `int` field named
  `profile` on `{"profile": "x"}`) expects `configuration_error` with `object_id` the
  allocation id and no ERROR record; green as above.
- Registration — Mode: focused-test. `tests/mcp/core/test_app.py`
  `test_server_fault_middleware_is_registered_innermost`.

Steps:
1. `git mv` the middleware and its test; update imports in `app.py`, `test_app.py`.
2. Write the new tests (red).
3. In `responses.py` add, above `InvalidEnvelopeError`:

```python
class ServerFaultError(Exception):
    """A value the server built failed validation — a server fault, not a caller error (ADR-0709).

    Deliberately not a ``ValueError``: pydantic would re-wrap one raised from a nested model into a
    ``ValidationError``, which FastMCP reports as the caller's argument error, and a tool's
    ``except ValueError`` for caller input could absorb it.
    """


def validate_stored[M: BaseModel](model: type[M], value: object) -> M:
    """Rebuild ``model`` from the server's own stored data (a database row, a recorded payload).

    Only for values the call's arguments did not supply: a failure is the server's fault and
    raises :class:`ServerFaultError` chained from the pydantic error (ADR-0709, #2981).
    """
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise ServerFaultError(f"stored {model.__name__} failed validation") from exc
```

   and change `class InvalidEnvelopeError(Exception)` to `class InvalidEnvelopeError(ServerFaultError)`.
4. In `server_fault.py` rename the class/constant, import `ServerFaultError`, and test
   `isinstance(exc.__cause__, ServerFaultError)`; update the module docstring.
5. Green, `just lint`, `just type`, commit.
6. Controlled fault: commit first, then change the middleware check back to
   `InvalidEnvelopeError`; the `stored` case goes red; `git checkout -- src/kdive/mcp/middleware/server_fault.py`.

## Task 2: allowlisted sites

Interfaces (consumed): `validate_stored` from Task 1.

Verification:
- Recorded-payload site — Mode: focused-test. `tests/mcp/core/test_responses.py`
  `test_recovery_response_with_malformed_recorded_payload_is_a_server_fault`: build a `Job` whose
  `payload` holds `{"recovery_request_v1": {"bogus": 1}}` and call
  `kdive.mcp.tools.external_boot.recovery_idempotency.recovery_response(job, "run_id", "r")`;
  expect `ServerFaultError`. Red: `ValidationError` raised instead; green:
  `just test-verbose tests/mcp/core/test_responses.py`.
- The other ten substitutions — Mode: task-test-not-applicable. Surface: the remaining sites in
  the spec table. Reason: each is the same one-line substitution inside a DB-backed read; seeding
  a malformed row per site through the testcontainer suites is disproportionate when the
  behaviour lives in `validate_stored` (Task 1) and the recorded-payload test proves the wiring
  once. Diff review against the spec table is the accepted Success 2 proof; the unchanged existing
  tool suites prove valid rows still rebuild.

Steps:
1. In each allowlisted function replace `X.model_validate(v)` with `validate_stored(X, v)` and
   add `validate_stored` to its `kdive.mcp.responses` import (`image_visibility.py` gains the
   import line `from kdive.mcp.responses import validate_stored`).
2. Leave `catalog/shapes.py`, `catalog/resources.py`, `lifecycle/allocations/view.py`,
   `lifecycle/support/_idempotency.py` untouched.
3. `just test-verbose tests/mcp/lifecycle tests/mcp/catalog tests/mcp/debug tests/mcp/ops tests/mcp/external_boot`
   expect pass; `just lint`, `just type`; commit.

## Task 3: ADR-0709 amendment

Verification: Mode: task-test-not-applicable. Surface: ADR prose. Reason: no executable
consumer validates amendment wording; `just records` checks record structure.

Steps: append `### Amendment (2026-09-29): server-built stored-data rebuilds are server faults
(#2981)` under Consequences stating the `ServerFaultError` family, `validate_stored`, the
allowlist boundary (stored data only; caller-input rebuilds keep `BindingErrorMiddleware`), and
the middleware rename. `just records`, commit.
