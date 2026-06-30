"""Doc-resource registrar: allowlist registration, drift, and packaging-failure (ADR-0151)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp import FastMCP

from kdive.mcp.resources import registrar
from kdive.mcp.resources.registrar import DOC_RESOURCES, audience_by_uri, register

_ROOT = Path(__file__).resolve().parents[3]


def test_doc_resources_default_to_all_audience_and_no_kind() -> None:
    for entry in DOC_RESOURCES:
        assert entry.audience in {"all", "operator"}
        if entry.name in {"external-build-upload", "build-source-staging", "response-envelope"}:
            assert entry.audience == "all"
            assert entry.required_kind is None


def test_audience_by_uri_covers_every_entry() -> None:
    mapping = audience_by_uri()
    assert set(mapping) == {entry.uri for entry in DOC_RESOURCES}
    assert all(v in {"all", "operator"} for v in mapping.values())


def test_register_returns_count_and_lists_every_uri_verbatim() -> None:
    app = FastMCP("probe")
    count = register(app)
    assert count == len(DOC_RESOURCES)

    async def _uris() -> set[str]:
        return {str(r.uri) for r in await app.list_resources()}

    listed = asyncio.run(_uris())
    # URIs must round-trip verbatim — a FastMCP scheme/host normalization would silently
    # change the advertised public contract.
    assert {e.uri for e in DOC_RESOURCES} <= listed


def test_each_resource_reads_back_canonical_doc_text() -> None:
    app = FastMCP("probe")
    register(app)

    async def _read(uri: str) -> str:
        result = await app.read_resource(uri)
        content = result.contents[0].content
        assert isinstance(content, str)
        return content

    for entry in DOC_RESOURCES:
        served = asyncio.run(_read(entry.uri))
        canonical = (_ROOT / entry.source).read_text(encoding="utf-8")
        assert served == canonical


def test_packaged_snapshot_matches_canonical_source() -> None:
    # Drift guard at the unit level: an edit to a canonical doc without regenerating the
    # snapshot fails here (not only in the `just resources-docs-check` shell recipe).
    for entry in DOC_RESOURCES:
        snapshot = (registrar._CONTENT_DIR / entry.content_file).read_text(encoding="utf-8")
        canonical = (_ROOT / entry.source).read_text(encoding="utf-8")
        assert snapshot == canonical, (
            f"{entry.content_file} drifted from {entry.source}; run 'just resources-docs'"
        )


def test_missing_snapshot_raises_packaging_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registrar, "_CONTENT_DIR", Path("/nonexistent/_content"))
    with pytest.raises(RuntimeError, match="snapshot missing"):
        register(FastMCP("probe"))
