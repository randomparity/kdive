"""gen_tool_reference: the pure registry → markdown core."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import scripts.gen_tool_reference as gen_tool_reference
from scripts.gen_tool_reference import ToolDoc, render_namespace, tool_docs


@dataclass
class _FakeAnn:
    readOnlyHint: bool | None = None
    destructiveHint: bool | None = None


@dataclass
class _FakeTool:
    name: str
    description: str | None
    parameters: dict
    annotations: _FakeAnn | None
    meta: dict


def _tool(name: str, **kw) -> _FakeTool:
    return _FakeTool(
        name=name,
        description=kw.get("description", "Does a thing."),
        parameters=kw.get("parameters", {"properties": {}}),
        annotations=kw.get("annotations", _FakeAnn(readOnlyHint=True)),
        meta=kw.get("meta", {"maturity": "implemented"}),
    )


def test_tool_docs_extracts_fields() -> None:
    docs = tool_docs([_tool("runs.get")])
    assert docs == [
        ToolDoc(
            name="runs.get",
            namespace="runs",
            description="Does a thing.",
            maturity="implemented",
            read_only=True,
            destructive=False,
            params=(),
        )
    ]


def test_render_is_deterministic_and_grouped() -> None:
    docs = tool_docs([_tool("runs.get"), _tool("runs.create", meta={"maturity": "partial"})])
    md = render_namespace("runs", docs)
    assert md.index("runs.create") < md.index("runs.get")  # sorted
    assert "do not edit" in md
    assert "partial" in md and "implemented" in md


def test_missing_description_raises() -> None:
    with pytest.raises(ValueError, match="no description"):
        tool_docs([_tool("runs.get", description="")])


def test_missing_maturity_raises() -> None:
    with pytest.raises(ValueError, match="maturity"):
        tool_docs([_tool("runs.get", meta={})])


def test_missing_param_description_raises() -> None:
    with pytest.raises(ValueError, match="no description"):
        tool_docs([_tool("runs.get", parameters={"properties": {"x": {"type": "string"}}})])


def test_param_description_with_pipe_raises() -> None:
    params = {"properties": {"x": {"type": "string", "description": "a | b"}}}
    with pytest.raises(ValueError, match="table-breaking"):
        tool_docs([_tool("runs.get", parameters=params)])


def test_write_reference_writes_namespace_and_index_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        gen_tool_reference,
        "_registry_tools",
        lambda: [_tool("jobs.wait"), _tool("runs.get")],
    )

    gen_tool_reference.write_reference(tmp_path)

    assert "`jobs.wait`" in (tmp_path / "jobs.md").read_text(encoding="utf-8")
    assert "`runs.get`" in (tmp_path / "runs.md").read_text(encoding="utf-8")
    assert "jobs.md#jobswait" in (tmp_path / "index.md").read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".*.md.*"))
