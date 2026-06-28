# Issue 891 Jobs Wait Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `jobs.wait` discoverable through normal wait/poll follow-up phrases in
gateway search.

**Architecture:** Keep the gateway ranking algorithm unchanged. Add curated jobs-plane
keywords to the existing `TOOL_KEYWORDS` map and cover the real `tools.search` behavior.

**Tech Stack:** Python 3.14, FastMCP gateway tests, pytest.

---

### Task 1: Add Jobs Search Keywords

**Files:**
- Modify: `tests/mcp/tools/test_gateway_search.py`
- Modify: `src/kdive/mcp/tool_index.py`

- [ ] **Step 1: Add failing gateway-search coverage**

In `tests/mcp/tools/test_gateway_search.py`, add a helper:

```python
def _match_names(content: dict[str, Any]) -> list[str]:
    return [m["name"] for m in content["data"]["matches"]]
```

Add a test that uses viewer context and checks several follow-up phrases:

```python
@pytest.mark.parametrize(
    "query",
    [
        "wait for job",
        "poll running job",
        "suggested next action jobs.wait",
    ],
)
def test_jobs_wait_discovered_from_followup_queries(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    import kdive.mcp.tools.gateway as gateway_module

    monkeypatch.setattr(gateway_module, "current_context", _viewer_ctx)

    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=_secret_registry())

    async def _run() -> Any:
        return await app.call_tool("tools.search", {"query": query})

    result = asyncio.run(_run())
    content = _call_result(result)
    assert content["status"] == "ok", f"expected ok, got {content}"
    assert "jobs.wait" in _match_names(content)
```

- [ ] **Step 2: Run the new test and verify it fails**

Run:

```bash
uv run python -m pytest tests/mcp/tools/test_gateway_search.py::test_jobs_wait_discovered_from_followup_queries -q
```

Expected: at least one query fails to include `jobs.wait` because jobs-plane curated
keywords are missing.

- [ ] **Step 3: Add jobs-plane curated keywords**

In `src/kdive/mcp/tool_index.py`, add a jobs section:

```python
    # jobs
    "jobs.get": frozenset({"job", "status", "get", "fetch", "lookup", "result"}),
    "jobs.list": frozenset({"jobs", "list", "filter", "background", "running"}),
    "jobs.wait": frozenset(
        {"job", "wait", "poll", "running", "retry", "complete", "terminal", "next", "action"}
    ),
    "jobs.cancel": frozenset({"job", "cancel", "stop", "abort", "running"}),
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run python -m pytest tests/mcp/tools/test_gateway_search.py tests/mcp/test_tool_index.py -q
```

Expected: gateway search and keyword completeness tests pass.

- [ ] **Step 5: Run relevant quality gates**

Run:

```bash
just lint
just type
```

Expected: both commands exit 0 with no warnings.
