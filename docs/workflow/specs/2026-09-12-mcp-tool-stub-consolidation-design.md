# MCP Tool stub consolidation

## Scope and authority

Issue #2380 and its accepted campaign scope authorize consolidating the two equivalent `Tool`
test doubles in `tests/mcp/`. The frozen scope token is `q2380-455df7d5`. Production code,
fixtures outside `tests/mcp/`, and an unconditional `model_copy` method are excluded.

## Decision

Add one `_FakeTool` fixture in `tests/mcp/conftest.py`. It supplies the name, description, schema,
and `model_copy` behavior needed by projection tests. Replace the local equivalent doubles with
that fixture. Keep gateway-search's `_StubTool` local because its absence of `model_copy` is the
passthrough contract under test.

## Validation

The affected projection modules pass with the shared fixture. The gateway-search tests retain a
controlled fault proving that projection is not invoked when `kinds` is absent. Run the targeted
tests, `just lint`, and `just type`.
