"""Doc-resource registrar: allowlist registration, drift, and packaging-failure (ADR-0151)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from fastmcp import FastMCP

from kdive.domain.catalog.resources import ResourceKind
from kdive.mcp.resources import registrar
from kdive.mcp.resources.registrar import DOC_RESOURCES, audience_by_uri, register
from kdive.providers.core.resolver import ProviderResolver

_ROOT = Path(__file__).resolve().parents[3]


class _FakeResolver:
    """Minimal ProviderResolver stand-in exposing only ``registered_kinds``.

    ``register`` calls only ``registered_kinds()``; building a real ``ProviderResolver``
    needs non-empty concrete runtimes, so the structural fake is cast at the call helper.
    """

    def __init__(self, kinds: frozenset[ResourceKind]) -> None:
        self._kinds = kinds

    def registered_kinds(self) -> frozenset[ResourceKind]:
        return self._kinds


def _resolver(kinds: Iterable[ResourceKind]) -> ProviderResolver:
    return cast(ProviderResolver, _FakeResolver(frozenset(kinds)))


_ALL_KINDS = _resolver(ResourceKind)


def test_doc_resources_default_to_all_audience_and_no_kind() -> None:
    for entry in DOC_RESOURCES:
        assert entry.audience in {"all", "operator"}
        if entry.name in {"external-build-upload", "build-source-staging", "response-envelope"}:
            assert entry.audience == "all"
            assert entry.required_kind is None


def test_audience_by_uri_covers_every_entry() -> None:
    from pydantic import AnyUrl

    mapping = audience_by_uri()
    # Keys are AnyUrl-normalized so the middleware's str(uri) lookup matches them exactly.
    assert set(mapping) == {str(AnyUrl(entry.uri)) for entry in DOC_RESOURCES}
    assert all(v in {"all", "operator"} for v in mapping.values())


def test_register_returns_count_and_lists_every_uri_verbatim() -> None:
    app = FastMCP("probe")
    count = register(app, resolver=_ALL_KINDS)
    assert count == len(DOC_RESOURCES)

    async def _uris() -> set[str]:
        return {str(r.uri) for r in await app.list_resources()}

    listed = asyncio.run(_uris())
    # URIs must round-trip verbatim — a FastMCP scheme/host normalization would silently
    # change the advertised public contract.
    assert {e.uri for e in DOC_RESOURCES} <= listed


def test_each_resource_reads_back_canonical_doc_text() -> None:
    app = FastMCP("probe")
    register(app, resolver=_ALL_KINDS)

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
        register(FastMCP("probe"), resolver=_ALL_KINDS)


def _gated_remote_doc() -> registrar.DocResource:
    """A fixture doc gated to remote-libvirt, reusing an existing snapshot file."""
    return replace(
        DOC_RESOURCES[0],
        uri="resource://kdive/docs/test/remote-only.md",
        name="remote-only",
        required_kind=ResourceKind.REMOTE_LIBVIRT,
    )


def test_register_skips_doc_whose_required_kind_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registrar, "DOC_RESOURCES", (*DOC_RESOURCES, _gated_remote_doc()))
    app = FastMCP("probe")
    count = register(app, resolver=_resolver({ResourceKind.LOCAL_LIBVIRT}))
    assert count == len(DOC_RESOURCES)  # the gated entry is skipped


def test_register_includes_doc_when_required_kind_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registrar, "DOC_RESOURCES", (*DOC_RESOURCES, _gated_remote_doc()))
    app = FastMCP("probe")
    count = register(app, resolver=_resolver({ResourceKind.REMOTE_LIBVIRT}))
    assert count == len(DOC_RESOURCES) + 1


_RESOURCE_URI = re.compile(r"resource://kdive/docs/[^\s)\"'>]+")
# A Markdown link whose target is a relative ``*.md`` path (optionally with a #fragment),
# excluding scheme URIs.
_RELATIVE_MD_LINK = re.compile(r"\]\((?!\w+://)([^)#]+\.md)(?:#[^)]*)?\)")


def test_served_doc_resource_citations_are_all_allowlisted() -> None:
    """Every ``resource://kdive/docs/...`` a served doc cites must itself be served (#1361).

    The flat allowlist has no path traversal, so a doc that points an agent at a
    ``resource://`` URI absent from ``DOC_RESOURCES`` is an unfetchable dead end — the class of
    bug F1 fixed. This guard fails if any served doc body cites a non-allowlisted resource URI.
    """
    allow = {entry.uri for entry in DOC_RESOURCES}
    offenders: list[str] = []
    for entry in DOC_RESOURCES:
        text = (_ROOT / entry.source).read_text(encoding="utf-8")
        for match in _RESOURCE_URI.findall(text):
            uri = match.rstrip(".,;")
            if uri not in allow:
                offenders.append(f"{entry.source} :: {uri}")
    assert not offenders, "served docs cite unfetchable resource URIs:\n" + "\n".join(offenders)


def test_served_docs_use_resource_uris_for_links_to_served_docs() -> None:
    """A served doc must not link to another *served* doc with a relative Markdown path (#1361).

    A relative ``*.md`` link is unfollowable over MCP (no path traversal), so when its target is
    itself a served resource the reader is stranded on a dead link to reachable content — exactly
    the F1 defect. The fix is to cite the target's ``resource://`` URI. Relative links to
    *unserved* targets (ADRs, human-only design docs) are fine and ignored here.
    """
    source_to_uri = {entry.source: entry.uri for entry in DOC_RESOURCES}
    offenders: list[str] = []
    for entry in DOC_RESOURCES:
        source = Path(entry.source)
        text = (_ROOT / entry.source).read_text(encoding="utf-8")
        for target in _RELATIVE_MD_LINK.findall(text):
            resolved = str((source.parent / target).resolve().relative_to(_ROOT.resolve()))
            if resolved in source_to_uri:
                offenders.append(f"{entry.source}: [{target}] -> cite {source_to_uri[resolved]}")
    assert not offenders, (
        "served docs link to served docs by relative path (cite the resource:// URI instead):\n"
        + "\n".join(offenders)
    )


def test_citation_guards_are_not_vacuous() -> None:
    # Canary: both matchers actually flag a bad citation/link, so a regex/read regression cannot
    # make the guards above pass by scanning nothing.
    allow = {"resource://kdive/docs/guide/errors.md"}
    text = "see resource://kdive/docs/guide/nonexistent.md for details"
    hits = [m.rstrip(".,;") for m in _RESOURCE_URI.findall(text) if m.rstrip(".,;") not in allow]
    assert hits == ["resource://kdive/docs/guide/nonexistent.md"]
    assert _RELATIVE_MD_LINK.findall("see [errors](errors.md#section) now") == ["errors.md"]
    assert _RELATIVE_MD_LINK.findall("see resource://kdive/docs/guide/errors.md") == []
