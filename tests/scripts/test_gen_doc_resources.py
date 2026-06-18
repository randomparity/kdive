"""Doc-resource snapshot generator behavior tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.mcp.resources.registrar import DocResource
from scripts import gen_doc_resources

_DOC = DocResource(
    uri="resource://kdive/test/doc",
    source="docs/example.md",
    content_file="example.md",
    name="example",
    title="Example",
    description="Example resource",
)


def test_write_copies_allowlisted_sources_to_content_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / _DOC.source).write_text("# Example\n", encoding="utf-8")
    content_dir = tmp_path / "content"
    monkeypatch.setattr(gen_doc_resources, "_ROOT", root)
    monkeypatch.setattr(gen_doc_resources, "DOC_RESOURCES", (_DOC,))

    gen_doc_resources.write(content_dir)

    assert (content_dir / _DOC.content_file).read_text(encoding="utf-8") == "# Example\n"


def test_check_reports_stale_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / _DOC.source).write_text("# Fresh\n", encoding="utf-8")
    content_dir = tmp_path / "content"
    content_dir.mkdir()
    (content_dir / _DOC.content_file).write_text("# Stale\n", encoding="utf-8")
    monkeypatch.setattr(gen_doc_resources, "_ROOT", root)
    monkeypatch.setattr(gen_doc_resources, "_CONTENT_DIR", content_dir)
    monkeypatch.setattr(gen_doc_resources, "DOC_RESOURCES", (_DOC,))

    assert gen_doc_resources.check() == 1

    assert "doc-resource snapshots are stale" in capsys.readouterr().err


def test_main_check_dispatches_without_rewriting_committed_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / _DOC.source).write_text("# Example\n", encoding="utf-8")
    content_dir = tmp_path / "content"
    content_dir.mkdir()
    (content_dir / _DOC.content_file).write_text("# Example\n", encoding="utf-8")
    monkeypatch.setattr(gen_doc_resources, "_ROOT", root)
    monkeypatch.setattr(gen_doc_resources, "_CONTENT_DIR", content_dir)
    monkeypatch.setattr(gen_doc_resources, "DOC_RESOURCES", (_DOC,))

    assert gen_doc_resources.main(["--check"]) == 0
    assert (content_dir / _DOC.content_file).read_text(encoding="utf-8") == "# Example\n"
