"""The shipped agent config examples must be valid JSON with an mcpServers.kdive entry."""

from __future__ import annotations

import json
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[2] / "docs" / "guide" / "agents"


def test_example_is_valid_json_with_kdive_server() -> None:
    data = json.loads((AGENTS / "mcp.json").read_text())
    assert "kdive" in data["mcpServers"]
