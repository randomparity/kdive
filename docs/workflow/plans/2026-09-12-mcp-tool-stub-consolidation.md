# MCP Tool stub consolidation (#2380)

**Spec:** `docs/workflow/specs/2026-09-12-mcp-tool-stub-consolidation-design.md`

## Task 1 — Share the equivalent projection stub

Create the MCP-local `_FakeTool` fixture with a default description and `model_copy` that preserves
the fixture's fields. Replace the two duplicated local classes and their constructors.

**Verification:** focused-test. Before the fixture exists, affected tests cannot import it; run
`just test-verbose tests/mcp/middleware/test_exposure_projection.py tests/mcp/test_gateway_projection.py`
after the change.

## Task 2 — Preserve the passthrough distinction

Leave `_StubTool` in `tests/mcp/tools/test_gateway_search.py` unchanged. Run its projection
passthrough tests and use the existing fault-injection test to prove the no-projection branch.

**Verification:** focused-test. Run
`just test-verbose tests/mcp/tools/test_gateway_search.py`.
