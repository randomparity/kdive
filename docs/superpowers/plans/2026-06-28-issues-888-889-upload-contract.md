# Issues 888 and 889 Upload Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `artifacts.create_run_upload` disclose required PUT headers and manifest
replacement semantics in the response and documentation.

**Architecture:** Keep the existing upload flow. Add explicit metadata to response data:
structured per-item `required_headers` and collection-level replacement mode fields.

**Tech Stack:** Python 3.14, FastMCP tool metadata, pytest, Markdown runbooks.

---

### Task 1: Surface Header and Manifest Contracts

**Files:**
- Modify: `tests/mcp/lifecycle/test_create_upload_tool.py`
- Modify: `tests/mcp/catalog/test_upload_declaration_schema.py`
- Modify: `src/kdive/mcp/tools/catalog/artifacts/uploads.py`
- Modify: `src/kdive/mcp/tools/catalog/artifacts/registrar.py`
- Modify: `docs/operating/external-build-upload.md`
- Modify: `src/kdive/mcp/resources/_content/external-build-upload.md`

- [ ] **Step 1: Add failing response assertions**

In `test_create_upload_mints_presigned_puts_and_persists_manifest`, after the existing item
data assertions, add:

```python
            assert responses.data["manifest_mode"] == "replace"
            assert responses.data["replaces_prior_manifest"] is True
            assert items[0].data["required_headers"] == {
                "x-amz-checksum-sha256": "aaa",
            }
            assert items[0].data["x-amz-checksum-sha256"] == "aaa"
```

- [ ] **Step 2: Add failing tool-description assertion**

In `tests/mcp/catalog/test_upload_declaration_schema.py`, add helper:

```python
def _tool_description(tool_name: str) -> str:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = FastMCP("upload-description-test")
    artifacts_registrar.register(app, pool, resolver=cast(ProviderResolver, object()))

    async def _description() -> str:
        tools = {tool.name: tool for tool in await app.list_tools()}
        return tools[tool_name].description or ""

    return asyncio.run(_description())
```

Add test:

```python
def test_create_run_upload_description_names_required_headers_and_replacement() -> None:
    description = _tool_description("artifacts.create_run_upload").lower()

    assert "required_headers" in description
    assert "replace" in description
    assert "manifest" in description
```

- [ ] **Step 3: Run new tests and verify they fail**

Run:

```bash
uv run python -m pytest tests/mcp/lifecycle/test_create_upload_tool.py::test_create_upload_mints_presigned_puts_and_persists_manifest tests/mcp/catalog/test_upload_declaration_schema.py::test_create_run_upload_description_names_required_headers_and_replacement -q
```

Expected: both fail because the response lacks the new data and the description lacks the
new contract wording.

- [ ] **Step 4: Add response data**

In `src/kdive/mcp/tools/catalog/artifacts/uploads.py`, change the collection response data:

```python
        data={
            "owner_kind": spec.owner_kind,
            "manifest_mode": "replace",
            "replaces_prior_manifest": True,
        },
```

In `_upload_response()`, add structured headers before the flattened fields:

```python
            "required_headers": dict(upload.presigned.required_headers),
```

- [ ] **Step 5: Update tool and runbook docs**

Change `artifacts_create_run_upload` docstring to mention that each upload item returns
`required_headers` for the PUT and that each call replaces the Run upload manifest.

In both runbook copies, update step 3/4 and the mismatch paragraph so they say:

- each response item contains `refs.upload_url` and `data.required_headers`;
- the PUT must include every returned required header;
- each `artifacts.create_run_upload` call replaces the previous manifest, so corrections
  must redeclare every artifact that should remain part of the build.

- [ ] **Step 6: Run focused tests**

Run:

```bash
uv run python -m pytest tests/mcp/lifecycle/test_create_upload_tool.py tests/mcp/catalog/test_upload_declaration_schema.py tests/db/test_upload_manifest.py -q
```

Expected: focused upload tests pass.

- [ ] **Step 7: Run relevant quality gates**

Run:

```bash
just lint
just type
```

Expected: both commands exit 0 with no warnings.
