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


def _tool_description(tool_name: str) -> str:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = FastMCP("upload-description-test")
    artifacts_registrar.register(app, pool, resolver=cast(ProviderResolver, object()))

    async def _description() -> str:
        tools = {tool.name: tool for tool in await app.list_tools()}
        return tools[tool_name].description or ""

    return asyncio.run(_description())


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


def test_create_run_upload_description_names_required_headers_and_replacement() -> None:
    description = _tool_description("artifacts.create_run_upload").lower()

    assert "required_headers" in description
    assert "replace" in description
    assert "manifest" in description


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


def test_system_tool_examples_are_single_put_identity_and_gzip() -> None:
    """Systems reject chunks (ADR-0436); the two examples are single-PUT identity + gzip (#1511)."""
    examples = _artifacts_examples("artifacts.create_system_upload")
    assert len(examples) == 2
    identity, gzipped = examples
    assert identity[0]["name"] == "rootfs"
    assert "chunks" not in identity[0]
    assert "encoding" not in identity[0]
    # The gzip example advertises the transport-encoding surface with its required companion.
    assert gzipped[0]["name"] == "rootfs"
    assert "chunks" not in gzipped[0]
    assert gzipped[0]["encoding"] == "gzip"
    assert isinstance(gzipped[0]["uncompressed_size"], int)
    # sha256/size_bytes describe the compressed bytes, which are smaller than the canonical object.
    assert gzipped[0]["size_bytes"] < gzipped[0]["uncompressed_size"]


def test_only_system_tool_advertises_transport_encoding_fields() -> None:
    """The systems item schema advertises encoding/uncompressed_size; the run schema does not.

    The transport encoding is a per-owner (rootfs) surface (ADR-0439): the run lane rejects a
    non-identity encoding at declaration, so advertising it there would invite a guaranteed
    rejection. This binds the per-owner schema split.
    """
    system_props = _artifacts_item_schema("artifacts.create_system_upload")["properties"]
    assert system_props["encoding"]["enum"] == ["gzip", "identity"]
    assert system_props["uncompressed_size"]["type"] == "integer"

    run_props = _artifacts_item_schema("artifacts.create_run_upload")["properties"]
    assert "encoding" not in run_props
    assert "uncompressed_size" not in run_props


def test_system_tool_description_documents_encoding_constraints() -> None:
    description = _tool_description("artifacts.create_system_upload").lower()
    assert "encoding" in description
    assert "gzip" in description
    assert "uncompressed_size" in description
    assert "50 gib" in description


def test_run_tool_description_states_encoding_not_accepted() -> None:
    description = _tool_description("artifacts.create_run_upload").lower()
    assert "encoding" in description
    assert "systems-only" in description or "rootfs" in description
