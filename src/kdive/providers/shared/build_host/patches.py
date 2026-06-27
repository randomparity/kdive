"""Patch no-op detection helpers shared by build-host implementations."""

from __future__ import annotations

from pathlib import Path


def patch_target_paths(patch_text: str, *, strip: int = 1) -> set[Path]:
    """Parse the workspace-relative file paths a unified diff touches.

    Collects both the pre-image (``--- a/...``) and post-image (``+++ b/...``) sides so
    created, modified, and deleted files are all covered, applying ``-p<strip>`` component
    stripping (``strip=1`` drops the leading ``a/``/``b/``). The ``/dev/null`` side of an
    add or delete, and any path shallower than ``strip``, are ignored.

    Used to verify ``git apply`` actually changed the tree: a ``.git``-less build workspace
    can make ``git apply`` exit 0 while silently skipping the patch (issue #227), so the
    caller snapshots these paths before and after applying and fails if none changed.
    """
    paths: set[Path] = set()
    for line in patch_text.splitlines():
        if not (line.startswith("--- ") or line.startswith("+++ ")):
            continue
        spec = line[4:].split("\t", 1)[0].strip()
        # git c-quotes paths with special/non-ASCII bytes ("b/...", octal escapes); decoding
        # them here would be brittle, so skip them — the caller's `git apply` stderr check
        # still catches a skipped quoted path, and we avoid wrongly flagging an applied one.
        if not spec or spec == "/dev/null" or spec.startswith('"'):
            continue
        components = spec.split("/")
        if len(components) <= strip:
            continue
        paths.add(Path(*components[strip:]))
    return paths


def snapshot_file_bytes(path: Path) -> bytes | None:
    """Return ``path`` contents, or ``None`` if it does not exist or cannot be read.

    Used by the build planes to snapshot a patch's target files before and after
    ``git apply`` and detect a silent no-op apply (issue #227).
    """
    try:
        return path.read_bytes()
    except OSError:
        return None
