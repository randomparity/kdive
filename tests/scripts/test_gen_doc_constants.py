"""Code-derived doc-constant generator/guard behavior tests (ADR-0410)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts import gen_doc_constants


def test_committed_constants_in_sync(capsys: pytest.CaptureFixture[str]) -> None:
    assert gen_doc_constants.main(["--check"]) == 0
    assert "doc constants:" in capsys.readouterr().out


def test_effective_ceiling_is_min_of_s3_cap_and_policy_limit() -> None:
    # Default KDIVE_MAX_UPLOAD_BYTES (50 GiB) is above the 5 GiB S3 single-PUT cap, so the
    # effective ceiling is the cap.
    assert gen_doc_constants._effective_single_put_ceiling() == "5 GiB"


def test_render_gib_rejects_non_multiple() -> None:
    assert gen_doc_constants._render_gib(3 * gen_doc_constants._GIB) == "3 GiB"
    with pytest.raises(ValueError, match="not an exact GiB multiple"):
        gen_doc_constants._render_gib(gen_doc_constants._GIB + 1)


def _writable_binding(path: Path, expected: str) -> gen_doc_constants.Binding:
    return gen_doc_constants.Binding(
        label="test count",
        path=path,
        pattern=re.compile(r"~(\d+) tools"),
        expected=expected,
        writable=True,
    )


def test_write_rewrites_stale_writable_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = tmp_path / "index.md"
    doc.write_text("uses the ~100 tools surfaced.\n", encoding="utf-8")
    monkeypatch.setattr(gen_doc_constants, "bindings", lambda: [_writable_binding(doc, "140")])

    gen_doc_constants.write()

    assert doc.read_text(encoding="utf-8") == "uses the ~140 tools surfaced.\n"


def test_check_flags_stale_writable_doc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tmp_path / "index.md"
    doc.write_text("uses the ~100 tools surfaced.\n", encoding="utf-8")
    monkeypatch.setattr(gen_doc_constants, "bindings", lambda: [_writable_binding(doc, "140")])

    assert gen_doc_constants.check() == 1

    err = capsys.readouterr().err
    assert "test count" in err
    assert "just doc-constants" in err


def test_check_guards_source_docstring_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A non-writable binding is guarded, not rewritten: the failure names the value to edit in.
    source = tmp_path / "registrar.py"
    source.write_text("larger than the 5 GiB single-PUT size limit.\n", encoding="utf-8")
    binding = gen_doc_constants.Binding(
        label="ceiling",
        path=source,
        pattern=re.compile(r"the (\d+ GiB) single-PUT size limit"),
        expected="4 GiB",
        writable=False,
    )
    monkeypatch.setattr(gen_doc_constants, "bindings", lambda: [binding])

    assert gen_doc_constants.check() == 1
    assert "edit" in capsys.readouterr().err
    # write() leaves a guarded (non-writable) source file untouched.
    gen_doc_constants.write()
    assert "5 GiB" in source.read_text(encoding="utf-8")


def test_check_flags_missing_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tmp_path / "index.md"
    doc.write_text("no tool-count phrase here.\n", encoding="utf-8")
    monkeypatch.setattr(gen_doc_constants, "bindings", lambda: [_writable_binding(doc, "140")])

    assert gen_doc_constants.check() == 1
    assert "no occurrence" in capsys.readouterr().err
