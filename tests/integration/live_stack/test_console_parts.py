"""Focused tests for marker-aware live console-part polling."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from kdive.mcp.responses import ToolResponse
from tests.integration.live_stack.console_parts import poll_for_new_console_part
from tests.integration.live_stack.spine import SpinePhaseError


class _FakeClient:
    def __init__(self, responses: list[ToolResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(self, name: str, **args: object) -> ToolResponse:
        self.calls.append((name, args))
        if not self._responses:
            raise AssertionError(f"unexpected {name} call with {args}")
        return self._responses.pop(0)


def _part(artifact_id: str, index: int) -> ToolResponse:
    return ToolResponse.success(
        artifact_id,
        "ready",
        refs={"object": f"local/systems/system-1/console-part-1-{index:06d}"},
    )


def _listing(*parts: ToolResponse) -> ToolResponse:
    return ToolResponse.collection("system-1", "ready", list(parts))


def _content(artifact_id: str, content: str) -> ToolResponse:
    return ToolResponse.success(
        artifact_id,
        "ready",
        data={"content": content, "content_truncated": False},
    )


def _live_client(client: _FakeClient) -> Any:
    return cast(Any, client)


def test_poll_skips_new_part_without_marker_and_returns_later_match() -> None:
    marker = "proof-marker"
    client = _FakeClient(
        [
            _listing(_part("early", 1), _part("old", 0)),
            _content("early", "boot output only"),
            _listing(_part("early", 1), _part("later", 2), _part("old", 0)),
            _content("later", f"prefix {marker} suffix"),
        ]
    )

    result = asyncio.run(
        poll_for_new_console_part(
            _live_client(client),
            "system-1",
            {"old"},
            marker,
            deadline_s=1.0,
            interval_s=0.0,
        )
    )

    assert result == ("later", f"prefix {marker} suffix")
    assert client.calls == [
        ("artifacts.list", {"system_id": "system-1"}),
        (
            "artifacts.get",
            {"request": {"artifact_id": "early", "byte_offset": 0}},
        ),
        ("artifacts.list", {"system_id": "system-1"}),
        (
            "artifacts.get",
            {"request": {"artifact_id": "later", "byte_offset": 0}},
        ),
    ]


def test_poll_times_out_after_inspecting_each_immutable_part_once() -> None:
    client = _FakeClient(
        [
            _listing(_part("early", 1), _part("old", 0)),
            _content("early", "boot output only"),
        ]
    )

    with pytest.raises(
        SpinePhaseError,
        match=r"no marker-bearing console-part artifacts within 0s .*inspected=1",
    ):
        asyncio.run(
            poll_for_new_console_part(
                _live_client(client),
                "system-1",
                {"old"},
                "proof-marker",
                deadline_s=0.0,
                interval_s=0.0,
            )
        )

    assert [call for call in client.calls if call[0] == "artifacts.get"] == [
        (
            "artifacts.get",
            {"request": {"artifact_id": "early", "byte_offset": 0}},
        )
    ]
