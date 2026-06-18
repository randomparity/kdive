"""Doc-resource snapshot generator behavior tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.mcp.resources.registrar import DOC_RESOURCES
from scripts import gen_doc_resources


def test_write_copies_allowlisted_sources_to_content_dir(tmp_path: Path) -> None:
    content_dir = tmp_path / "content"

    gen_doc_resources.write(content_dir)

    for doc_resource in DOC_RESOURCES:
        expected = Path(doc_resource.source).read_text(encoding="utf-8")
        assert (content_dir / doc_resource.content_file).read_text(encoding="utf-8") == expected


def test_check_reports_stale_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    content_dir = tmp_path / "content"
    gen_doc_resources.write(content_dir)
    stale_resource = DOC_RESOURCES[0]
    (content_dir / stale_resource.content_file).write_text("# Stale\n", encoding="utf-8")
    monkeypatch.setattr(gen_doc_resources, "_CONTENT_DIR", content_dir)

    assert gen_doc_resources.check() == 1

    assert "doc-resource snapshots are stale" in capsys.readouterr().err


def test_main_check_reports_committed_snapshots_in_sync(capsys: pytest.CaptureFixture[str]) -> None:
    assert gen_doc_resources.main(["--check"]) == 0
    assert "doc-resource snapshots:" in capsys.readouterr().out
