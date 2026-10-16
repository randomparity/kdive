"""Canonical Run/System/Job filtering-page controls (#3097)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kdive.mcp.responses import ToolResponse
from tests.integration.live_stack import tool_cells
from tests.integration.live_stack.test_tool_cells import (
    _GRANTS,
    _ITEM,
    _control,
    _Filtering,
    _list_run,
    _snapshot,
)
from tests.integration.live_stack.tool_cells import Rejection, prove_rejection


@pytest.mark.parametrize(
    "tool,actions",
    [
        ("runs.list", ["runs.get", "runs.create"]),
        ("systems.list", ["systems.get", "runs.create"]),
        ("jobs.list", ["jobs.wait", "jobs.cancel"]),
    ],
)
def test_canonical_pages_and_controls(tool: str, actions: list[str], tmp_path: Path) -> None:
    expected = ToolResponse.collection(
        tool.split(".")[0],
        "ok",
        [],
        suggested_next_actions=actions,
        data={"truncated": False, "next_cursor": None},
    )
    assert tool_cells._empty_page(tool, {}) == expected
    visible = expected.model_copy(update={"items": [ToolResponse.success(_ITEM, "ok")]})
    rejection = Rejection({"request": {}}, _GRANTS, filtered=_control())
    for bad in ("control", "metadata"):
        caller = _Filtering("gateway", expected, control=visible)
        if bad == "control":
            caller.control = expected
        else:
            caller.result = expected.model_copy(
                update={"data": {"truncated": True, "next_cursor": None}}
            )
        with pytest.raises(AssertionError, match="known item|empty page"):
            asyncio.run(
                prove_rejection(
                    _list_run(tmp_path, tool), caller, "authorization", rejection, _snapshot(1, 1)
                )
            )
    caller = _Filtering("gateway", expected, control=visible)
    asyncio.run(
        prove_rejection(
            _list_run(tmp_path, tool), caller, "authorization", rejection, _snapshot(1, 1)
        )
    )
