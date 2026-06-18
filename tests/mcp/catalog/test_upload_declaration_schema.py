"""The upload-declaration item input schema is discoverable (#567, ADR-0173).

`artifacts.create_run_upload` / `create_system_upload` advertise the declaration item
shape (required ``name``/``sha256``/``size_bytes``, optional ``chunks`` of per-chunk
``sha256``/``size_bytes``) on the ``artifacts`` parameter via ``json_schema_extra``,
while keeping the runtime type a permissive ``Mapping`` so declarations still reach the
ADR-0166 self-correcting validators. These tests read the live FastMCP input schema and a
drift guard binds the advertised ``required`` to the validator's required-field tuple.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.catalog.artifacts import registrar as artifacts_registrar
from kdive.mcp.tools.catalog.artifacts.uploads import _REQUIRED_DECLARATION_FIELDS
from kdive.providers.core.resolver import ProviderResolver

_UPLOAD_TOOLS = ("artifacts.create_run_upload", "artifacts.create_system_upload")


def _artifacts_item_schema(tool_name: str) -> dict[str, Any]:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = FastMCP("upload-declaration-schema-test")
    artifacts_registrar.register(app, pool, resolver=cast(ProviderResolver, object()))

    async def _collect() -> dict[str, Any]:
        tools = {tool.name: tool for tool in await app.list_tools()}
        artifacts = tools[tool_name].parameters["properties"]["artifacts"]
        return cast(dict[str, Any], artifacts)

    artifacts = asyncio.run(_collect())
    assert artifacts["type"] == "array"
    return cast(dict[str, Any], artifacts["items"])


def test_both_upload_tools_advertise_fielded_declaration_items() -> None:
    for tool_name in _UPLOAD_TOOLS:
        item = _artifacts_item_schema(tool_name)
        assert item["type"] == "object"
        props = item["properties"]
        assert props["name"]["type"] == "string"
        assert props["sha256"]["type"] == "string"
        assert props["size_bytes"]["type"] == "integer"
        assert set(item["required"]) == set(_REQUIRED_DECLARATION_FIELDS)


def test_chunks_subschema_exposes_per_chunk_fields() -> None:
    for tool_name in _UPLOAD_TOOLS:
        item = _artifacts_item_schema(tool_name)
        chunks = item["properties"]["chunks"]
        assert chunks["type"] == "array"
        chunk = chunks["items"]
        assert chunk["type"] == "object"
        assert chunk["properties"]["sha256"]["type"] == "string"
        assert chunk["properties"]["size_bytes"]["type"] == "integer"
        assert set(chunk["required"]) == {"sha256", "size_bytes"}


def test_declaration_required_drift_guard() -> None:
    """The advertised ``required`` cannot drift from what the validator enforces."""
    for tool_name in _UPLOAD_TOOLS:
        item = _artifacts_item_schema(tool_name)
        assert sorted(item["required"]) == sorted(_REQUIRED_DECLARATION_FIELDS)


def _artifacts_examples(tool_name: str) -> list[Any]:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = FastMCP("upload-declaration-examples-test")
    artifacts_registrar.register(app, pool, resolver=cast(ProviderResolver, object()))

    async def _examples() -> list[Any]:
        tools = {tool.name: tool for tool in await app.list_tools()}
        artifacts = tools[tool_name].parameters["properties"]["artifacts"]
        return cast(list[Any], artifacts["examples"])

    return asyncio.run(_examples())


def test_run_tool_carries_single_put_and_chunked_examples() -> None:
    examples = _artifacts_examples("artifacts.create_run_upload")
    assert len(examples) == 2
    single, chunked = examples
    assert "chunks" not in single[0]
    assert "chunks" in chunked[0]
    # The run tool advertises run-vocabulary names, not the system 'rootfs' name.
    assert single[0]["name"] == "kernel"


def test_system_tool_examples_use_rootfs_vocabulary() -> None:
    examples = _artifacts_examples("artifacts.create_system_upload")
    assert len(examples) == 2
    single, chunked = examples
    assert single[0]["name"] == "rootfs"
    assert chunked[0]["name"] == "rootfs"
    assert "chunks" in chunked[0]
