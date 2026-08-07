"""Guard: disposable-backend test fixture images are pinned to digests, not bare tags (#1921).

`tests/db/conftest.py` and `tests/store/conftest.py` each declare a module-level image
constant (``_POSTGRES_IMAGE``, ``_MINIO_IMAGE``). A mutable tag like ``postgres:17``
silently shifts to a new minor release on re-pull without any diff to explain it — the
schema-test gate becomes an assertion about an unknown Postgres version. Pinning to a
manifest-list digest makes the backend version part of the tree, so a pull that lands a
different image is impossible without a code change.

The guard enforces one invariant from the tree alone: every image constant in the two
conftest files carries an ``@sha256:<64-hex>`` digest. It does not verify that the digest
resolves correctly upstream — that check needs network access and stays a review-time
concern (ADR-0505). Keeping a human-readable tag in a trailing comment (``# 17``,
``# RELEASE.2025-09-07T16-13-09Z``) is the ADR-0505 convention and is strongly encouraged
but not enforced here; the digest is load-bearing, the comment is informational.

Stdlib + pytest only: this reads the tree, not the project.
"""

from __future__ import annotations

import re
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]

#: ``name@sha256:<64 hex digits>`` — an immutably-pinned image reference.
_DIGEST_PIN = re.compile(r"@sha256:[0-9a-f]{64}")

# The two fixture files and the constant they must each contain.
_FIXTURES: list[tuple[Path, str]] = [
    (_TESTS / "db" / "conftest.py", "_POSTGRES_IMAGE"),
    (_TESTS / "store" / "conftest.py", "_MINIO_IMAGE"),
]


def _image_value(text: str, constant: str) -> str | None:
    """Return the string value of a module-level ``constant = "..."`` assignment, or None.

    Handles both inline and parenthesized forms::

        _FOO = "value"
        _FOO = (
            "value"  # optional comment
        )
    """
    # Inline: _CONSTANT = "value"
    match = re.search(
        rf'^{re.escape(constant)}\s*=\s*"(?P<value>[^"]+)"',
        text,
        re.MULTILINE,
    )
    if match:
        return match.group("value")
    # Parenthesized: _CONSTANT = (\n    "value"\n)
    match = re.search(
        rf'^{re.escape(constant)}\s*=\s*\(\s*\n\s*"(?P<value>[^"]+)"',
        text,
        re.MULTILINE,
    )
    return match.group("value") if match else None


def test_fixture_image_constants_are_discoverable() -> None:
    # A rename, reformat, or regex change that stops matching would make the digest assertion
    # below pass over nothing, so verify that each constant can be found first.
    missing: list[str] = []
    for path, constant in _FIXTURES:
        text = path.read_text(encoding="utf-8")
        if _image_value(text, constant) is None:
            missing.append(f"{path.relative_to(_TESTS.parent)}: {constant!r} not found")
    assert not missing, (
        "image constant not found in fixture file — the constant was renamed or the "
        f"file moved, and this guard is now vacuous. Missing: {missing}"
    )


def test_fixture_images_are_digest_pinned() -> None:
    unpinned: list[str] = []
    for path, constant in _FIXTURES:
        text = path.read_text(encoding="utf-8")
        value = _image_value(text, constant)
        if value is None:
            continue  # caught by the discoverability test above
        if not _DIGEST_PIN.search(value):
            unpinned.append(
                f"{path.relative_to(_TESTS.parent)}: {constant} = {value!r} "
                f"(must contain @sha256:<64 hex digits>)"
            )
    assert not unpinned, (
        "test fixture image constants must be pinned to an immutable digest "
        "(``image:tag@sha256:<64hex>``), not a bare tag. A bare tag shifts silently "
        "on re-pull; a digest makes the backend version part of the tree. "
        "To update: `docker pull <tag>` and replace the digest with the new one. "
        f"Unpinned: {unpinned}"
    )
