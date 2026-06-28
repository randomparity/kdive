# Issue 896 Diagnostics Version Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose kdive service version and commit metadata on `ops.diagnostics` responses.

**Architecture:** Reuse `kdive.version` and project the data into the existing diagnostics
collection envelope. Do not add a new tool, audit event, or check item.

**Tech Stack:** Python 3.14, pytest, existing `ToolResponse` collection data.

---

### Task 1: Add Service Version Metadata To Diagnostics

**Files:**
- Modify: `tests/mcp/ops/test_diagnostics.py`
- Modify: `src/kdive/mcp/tools/ops/diagnostics.py`

- [x] **Step 1: Add failing response-shape coverage**

In `tests/mcp/ops/test_diagnostics.py`, import `VersionInfo` from `kdive.version` and add a
test in the verdict-shape section:

```python
def test_verdict_projects_service_version(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        diagnostics.service_version,
        "version_info",
        lambda: VersionInfo("1.2.3", "abc1234", False),
    )
    monkeypatch.setattr(diagnostics.service_version, "full_version", lambda: "1.2.3-dev+gabc1234")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await diagnostics.run_diagnostics(pool, _factory(_HEALTHY), _OPERATOR)
        assert resp.data["service_version"] == {
            "version": "1.2.3",
            "commit": "abc1234",
            "is_release": False,
            "full_version": "1.2.3-dev+gabc1234",
        }

    asyncio.run(_run())
```

- [x] **Step 2: Run the new test and verify it fails**

Run:

```bash
uv run python -m pytest tests/mcp/ops/test_diagnostics.py::test_verdict_projects_service_version -q
```

Expected: fails because `service_version` is not present in diagnostics response data.

- [x] **Step 3: Add service-version projection**

In `src/kdive/mcp/tools/ops/diagnostics.py`, import the version module:

```python
from kdive import version as service_version
```

Add a helper near `_verdict()`:

```python
def _service_version_data() -> dict[str, object]:
    info = service_version.version_info()
    return {
        "version": info.version,
        "commit": info.commit,
        "is_release": info.is_release,
        "full_version": service_version.full_version(),
    }
```

Include it in `_verdict()` top-level data:

```python
            "service_version": _service_version_data(),
```

- [x] **Step 4: Run focused diagnostics tests**

Run:

```bash
uv run python -m pytest tests/mcp/ops/test_diagnostics.py -q
```

Expected: diagnostics tests pass.

- [x] **Step 5: Run relevant quality gates**

Run:

```bash
just lint
just type
```

Expected: both commands exit 0 with no warnings.
