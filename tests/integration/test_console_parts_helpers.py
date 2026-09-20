"""Fast behavioral coverage for the console-parts live proof helpers."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from kdive.mcp.dev_harness import LiveStackClient
from kdive.mcp.responses import ToolResponse
from tests.integration import test_console_parts_live as subject


def test_full_text_nests_artifact_request_and_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    responses = iter(
        (
            ToolResponse.success(
                "artifact-1",
                "ready",
                data={"content": "first", "content_truncated": True, "next_offset": 5},
            ),
            ToolResponse.success(
                "artifact-1",
                "ready",
                data={"content": "second", "content_truncated": False},
            ),
        )
    )

    async def fake_scalar(_client: LiveStackClient, name: str, **args: object) -> ToolResponse:
        calls.append((name, args))
        return next(responses)

    monkeypatch.setattr(subject, "scalar", fake_scalar)

    result = asyncio.run(
        subject._full_text(cast(LiveStackClient, object()), "artifact-1", "read-artifact")
    )

    assert result == "firstsecond"
    assert calls == [
        (
            "artifacts.get",
            {"request": {"artifact_id": "artifact-1", "byte_offset": 0}},
        ),
        (
            "artifacts.get",
            {"request": {"artifact_id": "artifact-1", "byte_offset": 5}},
        ),
    ]
