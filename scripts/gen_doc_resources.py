"""Generate the packaged doc-resource snapshots from the canonical ``docs/`` tree (ADR-0151).

Run via ``just resources-docs`` (write) / ``just resources-docs-check`` (verify). The
allowlist lives in :data:`kdive.mcp.resources.registrar.DOC_RESOURCES` so the generator and
the registrar cannot diverge.

Each canonical source doc is read with UTF-8 ``read_text`` and written with ``write_text``,
so the snapshot is identical under the repo's text-normalizing pre-commit hooks. ``--check``
regenerates into a temp dir and diffs against the committed snapshots, exiting non-zero on
drift.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from kdive.mcp.resources.registrar import DOC_RESOURCES

_ROOT = Path(__file__).resolve().parents[1]
_CONTENT_DIR = _ROOT / "src" / "kdive" / "mcp" / "resources" / "_content"


def _source_text(source: str) -> str:
    path = _ROOT / source
    if not path.is_file():
        raise SystemExit(f"canonical doc missing: {source} (ADR-0151 allowlist is stale)")
    return path.read_text(encoding="utf-8")


def write(content_dir: Path) -> None:
    """Write each allowlisted source doc's snapshot into ``content_dir``."""
    content_dir.mkdir(parents=True, exist_ok=True)
    for entry in DOC_RESOURCES:
        (content_dir / entry.content_file).write_text(_source_text(entry.source), encoding="utf-8")


def check() -> int:
    """Regenerate into a temp dir and diff against the committed snapshots.

    Returns 0 when every committed snapshot matches its freshly generated form, 1 otherwise.
    """
    stale: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        write(tmp_dir)
        for entry in DOC_RESOURCES:
            committed = _CONTENT_DIR / entry.content_file
            fresh = tmp_dir / entry.content_file
            if not committed.is_file() or committed.read_text(encoding="utf-8") != fresh.read_text(
                encoding="utf-8"
            ):
                stale.append(entry.content_file)
    if stale:
        print(
            "doc-resource snapshots are stale — run 'just resources-docs' and commit: "
            + ", ".join(stale),
            file=sys.stderr,
        )
        return 1
    print(f"doc-resource snapshots: {len(DOC_RESOURCES)} in sync.")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--check":
        return check()
    write(_CONTENT_DIR)
    print(f"wrote {len(DOC_RESOURCES)} doc-resource snapshots to {_CONTENT_DIR}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
