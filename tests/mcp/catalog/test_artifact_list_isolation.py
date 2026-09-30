"""artifacts.list isolates one row whose envelope is invalid (ADR-0019, ADR-0709)."""

from __future__ import annotations

from typing import Any

import pytest

from kdive.mcp.responses import InvalidEnvelopeError, ToolResponse
from kdive.mcp.tools.catalog.artifacts import reads
from kdive.services.artifacts.listing import RedactedArtifact


def test_invalid_row_envelope_is_dropped_and_the_rest_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_success = ToolResponse.success

    def _success(object_id: str, status: str, **kwargs: Any) -> ToolResponse:
        if object_id == "bad":
            raise InvalidEnvelopeError("forced")
        return real_success(object_id, status, **kwargs)

    monkeypatch.setattr(reads.ToolResponse, "success", _success)
    items = reads._artifact_list_items(
        [RedactedArtifact("bad", "k/bad"), RedactedArtifact("good", "k/good")]
    )

    assert [item.object_id for item in items] == ["good"]
