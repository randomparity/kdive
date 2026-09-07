#!/usr/bin/env python3
"""Verify an installed Ansible collection tree exactly matches its requirements manifest."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


def _requirements(text: str) -> dict[str, str]:
    document: object = yaml.safe_load(text)
    collections = document.get("collections") if isinstance(document, dict) else None
    if not isinstance(collections, list):
        raise ValueError("requirements must contain a collections list")

    expected: dict[str, str] = {}
    for item in collections:
        if not isinstance(item, dict):
            raise ValueError("each requirement must be a mapping")
        name = item.get("name")
        version = item.get("version")
        if not isinstance(name, str) or name.count(".") != 1:
            raise ValueError(f"invalid collection name: {name!r}")
        if not isinstance(version, str) or not version.startswith("==") or len(version) == 2:
            raise ValueError(f"{name}: version must be an exact == pin")
        if name in expected:
            raise ValueError(f"duplicate collection requirement: {name}")
        expected[name] = version[2:]
    if not expected:
        raise ValueError("requirements collections list must not be empty")
    return expected


def _installed(root: Path) -> tuple[dict[str, str], list[str]]:
    base = root / "ansible_collections"
    if not base.is_dir():
        return {}, [f"installed collection root is missing: {base}"]

    installed: dict[str, str] = {}
    violations: list[str] = []
    for namespace in sorted(path for path in base.iterdir() if path.is_dir()):
        if namespace.name == "__pycache__":
            continue
        for collection in sorted(path for path in namespace.iterdir() if path.is_dir()):
            if collection.name == "__pycache__":
                continue
            path_name = f"{namespace.name}.{collection.name}"
            manifest_path = collection / "MANIFEST.json"
            if not manifest_path.is_file():
                violations.append(f"{path_name}: MANIFEST.json is missing")
                continue
            document: object = json.loads(manifest_path.read_text(encoding="utf-8"))
            info = document.get("collection_info") if isinstance(document, dict) else None
            if not isinstance(info, dict):
                violations.append(f"{path_name}: MANIFEST.json has no collection_info mapping")
                continue
            manifest_name = f"{info.get('namespace')}.{info.get('name')}"
            version = info.get("version")
            if manifest_name != path_name or not isinstance(version, str):
                violations.append(
                    f"{path_name}: invalid manifest identity/version "
                    f"({manifest_name!r}, {version!r})"
                )
                continue
            installed[path_name] = version
    return installed, violations


def evaluate(requirements_text: str, collections_root: Path) -> list[str]:
    """Return exact-set/version violations for an installed collection tree."""
    expected = _requirements(requirements_text)
    installed, violations = _installed(collections_root)
    for name in sorted(expected.keys() - installed.keys()):
        violations.append(f"{name}: required collection is missing")
    for name in sorted(installed.keys() - expected.keys()):
        violations.append(f"{name}: unexpected collection is installed")
    for name in sorted(expected.keys() & installed.keys()):
        if installed[name] != expected[name]:
            violations.append(
                f"{name}: installed version {installed[name]!r} != required {expected[name]!r}"
            )
    return violations


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} REQUIREMENTS COLLECTIONS_ROOT", file=sys.stderr)
        return 2
    requirements_path = Path(argv[1])
    try:
        violations = evaluate(
            requirements_path.read_text(encoding="utf-8"),
            Path(argv[2]),
        )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"Ansible collection verification failed: {exc}", file=sys.stderr)
        return 2
    for violation in violations:
        print(f"Ansible collection verification failed: {violation}", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
