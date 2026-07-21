# tests/scripts/test_check_served_doc_links.py
"""Behavioral tests for scripts/check-served-doc-links.sh.

DOC_RESOURCES (src/kdive/mcp/resources/registrar.py) is a flat resource:// allowlist with no
path traversal. A relative markdown link from one served doc to another is filesystem-valid —
check-doc-links.sh happily passes it — but unfetchable over MCP, since an agent only ever sees
the resource:// listing, never the docs/ filesystem tree. That was finding F1 (#1361,
ADR-0403); this script (#1364) is the regression guard.

The script always imports the real DOC_RESOURCES (via `uv run --project` rooted at the repo,
not at the caller's ROOT), so these tests build a tmp docs/ tree using the actual source paths
of a couple of entries and rely on the script's must-exist skip for every entry the test
doesn't populate (existence of every entry is `resources-docs-check`'s job, not this one's).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from tests.host_capabilities import requires_bash

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-served-doc-links.sh"
BASH = shutil.which("bash")

# The script reads matches with `mapfile` (bash >= 4.0).
pytestmark = requires_bash(4, 0, "mapfile")


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_resource_uri_citation_passes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/guide/response-envelope.md",
        "see the errors guide (resource://kdive/docs/guide/errors.md) for the taxonomy\n",
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_relative_link_to_served_doc_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/guide/response-envelope.md",
        "see the [errors guide](errors.md) for the taxonomy\n",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "docs/guide/response-envelope.md:1" in result.stderr
    assert "resource://kdive/docs/guide/errors.md" in result.stderr


def test_relative_link_to_unserved_doc_ignored(tmp_path: Path) -> None:
    # ADRs are deliberately not served (ADR-0270); a relative link to one is out of scope.
    _write(
        tmp_path,
        "docs/guide/response-envelope.md",
        "([ADR-0019](../adr/0019-tool-response-envelope.md))\n",
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_external_and_anchor_only_links_ignored(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/guide/response-envelope.md",
        "[x](https://example.com) [y](#section)\n",
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_links_inside_code_fences_ignored(tmp_path: Path) -> None:
    # \x60 is the backtick byte; three of them form a fence without a literal fence marker
    # living in this test file.
    fence = "\x60\x60\x60"
    _write(
        tmp_path,
        "docs/guide/response-envelope.md",
        f"{fence}\nsee [gone](errors.md)\n{fence}\n",
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_absent_served_doc_is_skipped_not_failed(tmp_path: Path) -> None:
    # Every DOC_RESOURCES entry is absent from this empty tree; existence is a different
    # gate's job (resources-docs-check), so this must pass rather than error.
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
