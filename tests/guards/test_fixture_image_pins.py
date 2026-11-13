"""Guard: disposable-backend test fixture images are pinned to digests, not bare tags (#1921).

`tests/db/conftest.py` and `tests/store/conftest.py` each declare a module-level image
constant (``_POSTGRES_IMAGE``, ``_SEAWEEDFS_IMAGE``). A mutable tag like ``postgres:17``
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

The same pin also governs every other Postgres image the tree starts against a fresh database
(the compose stack, the compose lifecycle-proof fixture and the Helm demo backend). A bare
``postgres:17`` there let an upstream re-tag to 17.12 break fresh migrations (#3610), so
those references must equal the db fixture's digest-pinned constant: the schema-test gate and
every stack migrate on one Postgres version, and a bump of one reference fails here until all
move together.

Stdlib, ``pyyaml`` and pytest: this reads the tree, not the project.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

_TESTS = Path(__file__).resolve().parents[1]

#: ``name@sha256:<64 hex digits>`` — an immutably-pinned image reference.
_DIGEST_PIN = re.compile(r"@sha256:[0-9a-f]{64}")

# The two fixture files and the constant they must each contain.
_FIXTURES: list[tuple[Path, str]] = [
    (_TESTS / "db" / "conftest.py", "_POSTGRES_IMAGE"),
    (_TESTS / "store" / "conftest.py", "_SEAWEEDFS_IMAGE"),
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


_ROOT = _TESTS.parent

#: Compose files whose ``postgres`` services must use the db fixture's pinned image.
_COMPOSE_FILES = (
    _ROOT / "docker-compose.yml",
    _TESTS / "compose" / "fixtures" / "worker-lifecycle-live.yml",
)
_HELM_VALUES = _ROOT / "deploy" / "helm" / "kdive" / "values.yaml"


def _stack_postgres_images() -> dict[str, str]:
    """Map ``<file>:<location>`` to each Postgres image reference the stacks start."""
    found: dict[str, str] = {}
    for path in _COMPOSE_FILES:
        services = yaml.safe_load(path.read_text(encoding="utf-8")).get("services", {})
        for name, service in services.items():
            image = service.get("image", "") if isinstance(service, dict) else ""
            repository = image.split("@", 1)[0].split(":", 1)[0]
            if repository.removeprefix("docker.io/").removeprefix("library/") == "postgres":
                found[f"{path.relative_to(_ROOT)}:services.{name}"] = image
    values = yaml.safe_load(_HELM_VALUES.read_text(encoding="utf-8"))
    found[f"{_HELM_VALUES.relative_to(_ROOT)}:demo.postgres.image"] = values["demo"]["postgres"][
        "image"
    ]
    return found


def test_stack_postgres_images_are_discoverable() -> None:
    # Two compose services per file plus the Helm demo backend; fewer means a rename or move
    # left this guard checking less than it claims.
    assert len(_stack_postgres_images()) == 5, _stack_postgres_images()


def test_stack_postgres_images_match_the_pinned_fixture() -> None:
    pinned = _image_value(
        (_TESTS / "db" / "conftest.py").read_text(encoding="utf-8"), "_POSTGRES_IMAGE"
    )
    assert pinned is not None and _DIGEST_PIN.search(pinned)
    drift = {where: image for where, image in _stack_postgres_images().items() if image != pinned}
    assert not drift, (
        f"every stack Postgres image must equal the db fixture pin {pinned!r} "
        "(tests/db/conftest.py::_POSTGRES_IMAGE), so an upstream re-tag cannot change the "
        "Postgres minor version a fresh database migrates on (#3610). Update all references "
        f"together. Drifted: {drift}"
    )


def _unpinned_container_calls() -> tuple[int, list[str]]:
    """Scan ``tests/**/*.py`` for ``PostgresContainer(...)`` calls.

    Returns the call-site count and a description of each call whose image argument is neither
    the ``_POSTGRES_IMAGE`` constant (bare or as a module attribute) nor a string literal
    carrying an ``@sha256:<64 hex>`` digest.
    """
    sites = 0
    unpinned: list[str] = []
    for path in sorted(_TESTS.rglob("*.py")):
        if path == Path(__file__).resolve():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")) != (
                "PostgresContainer"
            ):
                continue
            sites += 1
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Name) and arg.id == "_POSTGRES_IMAGE":
                continue
            if isinstance(arg, ast.Attribute) and arg.attr == "_POSTGRES_IMAGE":
                continue
            if (
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and _DIGEST_PIN.search(arg.value)
            ):
                continue
            unpinned.append(f"{path.relative_to(_ROOT)}:{node.lineno}")
    return sites, unpinned


def test_postgres_container_call_sites_are_discoverable() -> None:
    # The db conftest session fixture and the URL-fixture roundtrip test; fewer means the scan
    # stopped seeing them and the pin assertion below is vacuous.
    sites, _ = _unpinned_container_calls()
    assert sites >= 2, f"expected at least 2 PostgresContainer call sites, found {sites}"


def test_postgres_containers_use_the_pinned_image() -> None:
    _, unpinned = _unpinned_container_calls()
    assert not unpinned, (
        "every PostgresContainer in tests/ must start from tests/db/conftest.py::_POSTGRES_IMAGE "
        "(or a literal with an @sha256:<64 hex> digest); a bare tag such as postgres:17 shifts "
        f"on upstream re-tag and broke fresh migrations (#3610). Unpinned: {unpinned}"
    )
