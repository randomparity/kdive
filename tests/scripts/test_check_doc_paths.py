# tests/scripts/test_check_doc_paths.py
"""Behavioral tests for scripts/check-doc-paths.sh.

The checker greps justfile / scripts / *.yml / *.md code spans for concrete
docs/<seg>/... references and fails when the target is missing. Illustrative
ellipses (docs/... , docs/…) must NOT be flagged.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.host_capabilities import requires_bash

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-doc-paths.sh"
BASH = shutil.which("bash")

pytestmark = requires_bash(3, 2, "portable documentation path guard")


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), str(root)], capture_output=True, text=True, check=False
    )


def test_existing_path_passes(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide").mkdir()
    (tmp_path / "docs" / "guide" / "index.md").write_text("hi\n")
    (tmp_path / "justfile").write_text("x:\n\techo docs/guide/index.md\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_missing_path_fails(tmp_path: Path) -> None:
    (tmp_path / "justfile").write_text("x:\n\techo docs/reports/m2-portability.md\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "docs/reports/m2-portability.md" in result.stderr


def test_illustrative_ellipsis_ignored(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("references `docs/<seg>/…` and `docs/...` are fine\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_frozen_design_records_not_scanned(tmp_path: Path) -> None:
    # docs/{design,archive} records may narrate moves or preserve history: their docs/... mentions
    # of missing/old paths must not fail the check. ADRs stay policed — they are a living decision
    # log, superseded rather than excluded.
    for d in ("design", "archive"):
        (tmp_path / "docs" / d).mkdir(parents=True)
    (tmp_path / "docs" / "design" / "spec.md").write_text("we move docs/specs to docs/design\n")
    (tmp_path / "docs" / "archive" / "old.md").write_text("see docs/plans/m0-implementation.md\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_paths_inside_code_fences_ignored(tmp_path: Path) -> None:
    # An operational doc may show an example command referencing a path in a code block;
    # that is a sample, not a live reference. \x60 is the backtick byte.
    fence = "\x60\x60\x60"
    (tmp_path / "a.md").write_text(f"{fence}\ncat docs/operating/not-yet.md\n{fence}\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_substring_docs_not_matched(tmp_path: Path) -> None:
    # The docs/ token is anchored on a left word boundary, so a substring inside a larger
    # word (mkdocs/, subdocs/) is not mistaken for a docs/ reference.
    (tmp_path / "a.md").write_text("see mkdocs/foo and subdocs/bar for the tool\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_changelog_not_scanned(tmp_path: Path) -> None:
    # CHANGELOG.md is git-cliff-generated and reproduces commit subjects verbatim, which
    # may contain docs/-prefixed recipe-name tokens (e.g. "docs/docs-check") that are not
    # filesystem paths. The generated changelog must not be policed for path existence.
    (tmp_path / "CHANGELOG.md").write_text("- Add just docs/docs-check and gate ci\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_vendored_tool_dirs_not_scanned(tmp_path: Path) -> None:
    # Vendored agent-tooling config (.claude/, .agents/, .codex/) is not project docs;
    # its illustrative docs/... example strings must not be policed.
    skill = tmp_path / ".claude" / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("e.g. your overlay doc `docs/nope.md`\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("tracked", [False, True], ids=["filesystem", "git"])
def test_current_architecture_is_checked(tmp_path: Path, tracked: bool) -> None:
    architecture = tmp_path / "docs" / "design" / "top-level-design.md"
    architecture.parent.mkdir(parents=True)
    architecture.write_text("See `docs/missing.md`.\n")
    (tmp_path / "README.md").write_text("Current project documentation.\n")
    if tracked:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    result = _run(tmp_path)
    assert result.returncode == 1, result.stdout
    assert "top-level-design.md" in result.stderr
    assert "docs/missing.md" in result.stderr


def _tool_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, body: str) -> None:
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    stub = tools / name
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")


@pytest.mark.parametrize("tool", ["git", "find", "tr", "awk"])
@pytest.mark.parametrize("partial", [False, True], ids=["empty", "partial"])
def test_tool_failure_cannot_certify_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, partial: bool
) -> None:
    (tmp_path / "justfile").write_text("# No references\n")
    output = "printf 'justfile\\n'\n" if partial else ""
    _tool_stub(tmp_path, monkeypatch, tool, output + "echo injected-tool-failure >&2\nexit 23")
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "injected-tool-failure" in result.stderr
    assert "doc paths resolve" not in result.stdout


@pytest.mark.parametrize("partial", [False, True], ids=["empty", "partial"])
def test_extractor_failure_cannot_certify_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial: bool
) -> None:
    (tmp_path / "justfile").write_text("# No references\n")
    real_awk = shutil.which("awk")
    assert real_awk is not None
    output = "printf 'docs/\\n'\n" if partial else ""
    _tool_stub(
        tmp_path,
        monkeypatch,
        "awk",
        f'case "$1" in *fence*) {output}echo injected-extractor-failure >&2; exit 23;; esac\n'
        f'exec {shlex.quote(real_awk)} "$@"',
    )
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "injected-extractor-failure" in result.stderr
    assert "justfile" in result.stderr
    assert "doc paths resolve" not in result.stdout


@pytest.mark.parametrize("tool,option", [("grep", "-oP"), ("find", "-printf")])
def test_missing_path_with_portable_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, option: str
) -> None:
    (tmp_path / "justfile").write_text("docs/missing.md\n")
    real_tool = shutil.which(tool)
    assert real_tool is not None
    _tool_stub(
        tmp_path,
        monkeypatch,
        tool,
        f'for arg do if [ "$arg" = {shlex.quote(option)} ]; then exit 23; fi; done\n'
        f'exec {shlex.quote(real_tool)} "$@"',
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "missing doc path: justfile references docs/missing.md" in result.stderr


def test_missing_path_without_mapfile(tmp_path: Path) -> None:
    (tmp_path / "justfile").write_text("docs/missing.md\n")
    assert BASH is not None
    result = subprocess.run(
        [
            BASH,
            "-c",
            'enable -n mapfile 2>/dev/null || :; source "$1" "$2"',
            "guard-test",
            str(SCRIPT),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "missing doc path: justfile references docs/missing.md" in result.stderr


@pytest.mark.parametrize("tracked", [False, True], ids=["filesystem", "git"])
@pytest.mark.parametrize("name", ["a b.md", "a\\b.md", "a\tb.md", 'a"b.md', "-a.md", "a=b.md"])
def test_source_filename_is_literal(tmp_path: Path, tracked: bool, name: str) -> None:
    (tmp_path / name).write_text("docs/missing.md\n")
    if tracked:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert f"{name} references docs/missing.md" in result.stderr


@pytest.mark.parametrize("text", ["", "No references\n", "docs/… docs/<seg>\n"])
def test_no_matches_pass(tmp_path: Path, text: str) -> None:
    (tmp_path / "a.md").write_text(text)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "doc paths resolve" in result.stdout


def test_multiple_references_keep_boundaries_and_deduplicate(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "xdocs/ignored /docs/ignored _docs/ignored .docs/ignored -docs/ignored\n"
        "(docs/one.md), docs/two.md. `docs/one.md`\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "ignored" not in result.stderr
    assert result.stderr.count("references docs/one.md\n") == 1
    assert result.stderr.count("references docs/two.md\n") == 1
