"""The fielded-outputSchema sweep that documents the ToolResponse envelope (#565, ADR-0170)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.schema_advertising import (
    ENVELOPE_OUTPUT_SCHEMA,
    advertise_envelope_output_schema,
)


def _probe_app() -> FastMCP:
    app: FastMCP = FastMCP(name="probe")

    @app.tool(name="scalar.one")
    def scalar_one() -> ToolResponse:
        return ToolResponse.success("obj-1", "ok", data={"k": "v"})

    @app.tool(name="list.coll")
    def list_coll() -> ToolResponse:
        return ToolResponse.collection("c", "ok", [ToolResponse.success("a", "ok")])

    return app


class _ErrorCollector(logging.Handler):
    """Capture ERROR records off the ``fastmcp`` logger.

    The FastMCP client logger sets ``propagate=False`` and uses its own handler, so pytest's
    ``caplog`` (a root-logger handler) does NOT see the parse error — verified. Attach directly.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _call_and_capture(
    app: FastMCP, tool: str
) -> tuple[object | None, list[str], dict[str, Any] | None]:
    """Call ``tool`` on ``app``.

    Returns ``(.data, structured-content parse-error messages, .structured_content)``. With the
    fielded schema ``.data`` is a pydantic model (not a dict); ``structured_content`` is the
    byte-stable envelope dict.
    """
    logger = logging.getLogger("fastmcp")
    handler = _ErrorCollector()
    logger.addHandler(handler)
    try:

        async def _call() -> tuple[object | None, dict[str, Any] | None]:
            async with Client(app) as client:
                result = await client.call_tool(tool, {})
                return result.data, result.structured_content

        data, structured = asyncio.run(_call())
    finally:
        logger.removeHandler(handler)
    errors = [r.getMessage() for r in handler.records if "structured content" in r.getMessage()]
    return data, errors, structured


def test_schema_advertises_every_envelope_field() -> None:
    # AC#1 + AC#3 drift guard: the advertised properties are exactly the model fields.
    assert ENVELOPE_OUTPUT_SCHEMA["type"] == "object"
    assert set(ENVELOPE_OUTPUT_SCHEMA["properties"]) == set(ToolResponse.model_fields)


def test_schema_is_ref_free() -> None:
    # AC#2: no recursion — the constant carries no $ref/$defs.
    serialized = json.dumps(ENVELOPE_OUTPUT_SCHEMA)
    assert "$ref" not in serialized
    assert "$defs" not in serialized


def test_sweep_advertises_fielded_schema() -> None:
    app = _probe_app()
    swept = advertise_envelope_output_schema(app)
    assert swept == 2

    async def _run() -> list[dict[str, Any] | None]:
        async with Client(app) as client:
            return [t.outputSchema for t in await client.list_tools()]

    for schema in asyncio.run(_run()):
        assert schema is not None
        assert set(schema["properties"]) == set(ToolResponse.model_fields)


def test_failure_detail_round_trips_through_client() -> None:
    # AC#2 surface: the `detail` field rides the structured-content payload unchanged.
    app: FastMCP = FastMCP(name="detail-probe")

    @app.tool(name="fail.one")
    def fail_one() -> ToolResponse:
        exc = CategorizedError(
            "invalid provisioning profile", category=ErrorCategory.CONFIGURATION_ERROR
        )
        return ToolResponse.failure_from_error("obj-1", exc)

    advertise_envelope_output_schema(app)
    data, errors, structured = _call_and_capture(app, "fail.one")
    assert data is not None
    assert structured is not None
    assert structured["detail"] == "invalid provisioning profile"
    assert errors == []


def test_sweep_restores_data_and_logs_no_parse_error() -> None:
    app = _probe_app()
    advertise_envelope_output_schema(app)
    data, errors, structured = _call_and_capture(app, "scalar.one")
    assert data is not None  # parse succeeded (model instance), not nulled
    assert structured is not None
    assert structured["object_id"] == "obj-1"  # structured_content restored
    assert errors == []  # no parse-error log


def test_collection_round_trips_through_client() -> None:
    # AC#2: a non-empty `items` envelope parses; structured_content keeps the nested list.
    app = _probe_app()
    advertise_envelope_output_schema(app)
    data, errors, structured = _call_and_capture(app, "list.coll")
    assert data is not None
    assert structured is not None
    assert isinstance(structured["items"], list)
    assert structured["items"]  # non-empty
    assert errors == []


def test_unswept_recursive_schema_now_parses_cleanly() -> None:
    """Historical regression pin, revisited for the FastMCP upgrade that handles recursive ``$ref``.

    Before FastMCP handled recursive ``$ref``, the auto-derived ``ToolResponse`` output schema broke
    the client parser (``.data`` nulled, a parse error logged) — the motivation for the sweep
    (``advertise_envelope_output_schema``, ADR-0170). FastMCP 3.4.4 parses the recursive schema
    cleanly, so an unswept call now succeeds. The sweep is retained for its fielded-schema
    advertising (see the sweep tests above); this pins the client-side change that removed its
    original parse-failure motivation.
    """
    app = _probe_app()  # NOT swept
    data, errors, _structured = _call_and_capture(app, "scalar.one")
    assert data is not None  # the recursive schema now parses cleanly
    assert errors == []  # no parse error logged


def test_sweep_raises_on_empty_tool_surface() -> None:
    """A zero count means the registry accessor broke — fail loud, don't ship recursive schemas."""
    empty: FastMCP = FastMCP(name="empty")
    with pytest.raises(RuntimeError):
        advertise_envelope_output_schema(empty)
