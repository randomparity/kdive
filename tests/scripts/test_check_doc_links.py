# tests/scripts/test_check_doc_links.py
"""Behavioral tests for scripts/check-doc-links.sh.

The checker resolves relative markdown links in tracked *.md files against the
filesystem. Tests build a tiny tree with a good and a broken link and assert the
exit status and that the broken target is named.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.host_capabilities import requires_bash

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-doc-links.sh"
BASH = shutil.which("bash")

pytestmark = requires_bash(3, 2, "portable Markdown link guard")


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_resolvable_links_pass(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("see [b](b.md)\n")
    (tmp_path / "b.md").write_text("hi\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_broken_link_fails_and_names_target(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("see [gone](missing.md)\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "missing.md" in result.stderr
    assert "a.md" in result.stderr


def test_external_and_anchor_only_links_ignored(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("[x](https://example.com) [y](#section)\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_links_inside_code_fences_ignored(tmp_path: Path) -> None:
    # A doc may show an example markdown link inside a code sample; that is not a real
    # cross-reference and must not be resolved. \x60 is the backtick byte; the three of
    # them form a fence without putting a literal fence marker in this test file.
    fence = "\x60\x60\x60"
    (tmp_path / "a.md").write_text(f"{fence}\nsee [gone](does-not-exist.md)\n{fence}\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_titled_link_resolves(tmp_path: Path) -> None:
    # CommonMark allows a title after the destination; it must not be treated as part
    # of the path.
    (tmp_path / "a.md").write_text('see [b](b.md "the title")\n')
    (tmp_path / "b.md").write_text("hi\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_design_and_archive_not_link_checked(tmp_path: Path) -> None:
    # Archived history is frozen (its links pointed at the old tree) and design specs are
    # narrative; neither must fail the link gate.
    (tmp_path / "docs" / "archive").mkdir(parents=True)
    (tmp_path / "docs" / "design").mkdir(parents=True)
    (tmp_path / "docs" / "archive" / "old.md").write_text("[x](../../adr/gone.md)\n")
    (tmp_path / "docs" / "design" / "spec.md").write_text("[y](does-not-exist.md)\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_vendored_tool_dirs_not_link_checked(tmp_path: Path) -> None:
    # Vendored agent-tooling config (.claude/, .agents/, .codex/) is not project docs;
    # its illustrative links must not be resolved against the filesystem.
    skill = tmp_path / ".agents" / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("[x](nope.md)\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("tracked", [False, True], ids=["filesystem", "git"])
def test_current_architecture_is_checked(tmp_path: Path, tracked: bool) -> None:
    architecture = tmp_path / "docs" / "design" / "top-level-design.md"
    architecture.parent.mkdir(parents=True)
    architecture.write_text("[missing](missing.md)\n")
    (tmp_path / "README.md").write_text("Current project documentation.\n")
    if tracked:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    result = _run(tmp_path)
    assert result.returncode == 1, result.stdout
    assert "top-level-design.md" in result.stderr
    assert "missing.md" in result.stderr


def _tool_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, body: str) -> None:
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    stub = tools / name
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")


@pytest.mark.parametrize("tool", ["git", "find", "tr", "awk"])
@pytest.mark.parametrize("partial", [False, True], ids=["empty", "partial"])
def test_tool_failure_cannot_certify_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, partial: bool
) -> None:
    (tmp_path / "a.md").write_text("No links\n")
    output = "printf 'a.md\\n'\n" if partial else ""
    _tool_stub(tmp_path, monkeypatch, tool, output + "echo injected-tool-failure >&2\nexit 23")
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "injected-tool-failure" in result.stderr
    assert "cannot" in result.stderr
    assert "markdown links resolve" not in result.stdout


@pytest.mark.parametrize("stage", ["extractor", "normalizer"])
@pytest.mark.parametrize("partial", [False, True], ids=["empty", "partial"])
def test_awk_stage_failure_cannot_certify_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, partial: bool
) -> None:
    (tmp_path / "a.md").write_text("No links\n")
    real_awk = shutil.which("awk")
    assert real_awk is not None
    pattern = "*fence*" if stage == "extractor" else "*sub*"
    output = "printf 'a.md\\n'\n" if partial else ""
    _tool_stub(
        tmp_path,
        monkeypatch,
        "awk",
        f'case "$1" in {pattern}) {output}echo injected-stage-failure >&2; exit 23;; esac\n'
        f'exec {shlex.quote(real_awk)} "$@"',
    )
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "injected-stage-failure" in result.stderr
    assert "cannot" in result.stderr
    if stage == "extractor":
        assert "a.md" in result.stderr
    assert "markdown links resolve" not in result.stdout


def test_broken_link_with_portable_find(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.md").write_text("[gone](missing.md)\n")
    real_find = shutil.which("find")
    assert real_find is not None
    _tool_stub(
        tmp_path,
        monkeypatch,
        "find",
        'for arg do if [ "$arg" = -printf ]; then exit 23; fi; done\n'
        f'exec {shlex.quote(real_find)} "$@"',
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "broken link: a.md -> missing.md" in result.stderr


def test_broken_link_without_mapfile(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("[gone](missing.md)\n")
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
    assert "broken link: a.md -> missing.md" in result.stderr


@pytest.mark.parametrize("tracked", [False, True], ids=["filesystem", "git"])
@pytest.mark.parametrize("name", ["a b.md", "a\\b.md", "a\tb.md", 'a"b.md', "-a.md", "a=b.md"])
def test_source_filename_is_literal(tmp_path: Path, tracked: bool, name: str) -> None:
    (tmp_path / name).write_text("[gone](missing.md)\n")
    if tracked:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert f"broken link: {name} -> missing.md" in result.stderr


@pytest.mark.parametrize("text", ["", "No links\n", "[empty]() [anchor](#here)\n"])
def test_no_matches_pass(tmp_path: Path, text: str) -> None:
    (tmp_path / "a.md").write_text(text)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "markdown links resolve" in result.stdout


def test_multiple_targets_preserve_existing_semantics(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        '[a](missing.md#fragment) [b](missing.md "title") [c](other.md)\n'
        "[mail](mailto:ignored) [url](https://example.com)\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert result.stderr.count("broken link: a.md -> missing.md\n") == 2
    assert result.stderr.count("broken link: a.md -> other.md\n") == 1
    assert "mailto" not in result.stderr
    assert "https:" not in result.stderr


def test_tracked_file_missing_from_worktree_fails(tmp_path: Path) -> None:
    source = tmp_path / "a.md"
    source.write_text("No links\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    source.unlink()
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "cannot extract markdown links from a.md" in result.stderr
    assert "markdown links resolve" not in result.stdout


@pytest.mark.parametrize("tracked", [False, True], ids=["filesystem", "git"])
def test_selection_and_exclusions_preserved(tmp_path: Path, tracked: bool) -> None:
    (tmp_path / "a.md").write_text("No links\n")
    for name in [
        Path("docs") / "archive" / "old.md",
        Path("docs") / "design" / "spec.md",
        ".claude/example.md",
        ".agents/example.md",
        ".codex/example.md",
        "src/kdive/mcp/resources/_content/example.md",
    ]:
        source = tmp_path / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("[gone](missing.md)\n")
    if tracked:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
        (tmp_path / "untracked.md").write_text("[gone](missing.md)\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_empty_tracked_selection_keeps_filesystem_fallback(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.md").write_text("[gone](missing.md)\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "broken link: a.md -> missing.md" in result.stderr


def test_converter_failure_is_not_git_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.md").write_text("No links\n")
    real_tr = shutil.which("tr")
    assert real_tr is not None
    _tool_stub(
        tmp_path,
        monkeypatch,
        "tr",
        f'{shlex.quote(real_tr)} "$@"\necho injected-converter-failure >&2\nexit 128',
    )
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "injected-converter-failure" in result.stderr
    assert "cannot enumerate" in result.stderr
    assert "markdown links resolve" not in result.stdout
