"""AST guard for immutable object-store deletion (ADR-0524).

Text matching cannot distinguish an S3 object-store delete from repository, snapshot, or
libvirt-volume deletion. This guard identifies store-like receivers structurally and reserves the
one raw boto3 ``delete_object`` call for ``ObjectStore.delete_version`` with ``VersionId``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _receiver_is_store_like(receiver: ast.expr) -> bool:
    def _looks_like_store(name: str) -> bool:
        normalized = name.lower().strip("_")
        return (
            normalized == "store"
            or normalized.endswith("_store")
            or normalized.startswith("store_")
        )

    if isinstance(receiver, ast.Name):
        return _looks_like_store(receiver.id)
    if isinstance(receiver, ast.Attribute):
        return _looks_like_store(receiver.attr)
    if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name):
        return _looks_like_store(receiver.func.id)
    return False


class _DeleteVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._classes: list[str] = []
        self._functions: list[str] = []
        self.violations: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._classes.append(node.name)
        self.generic_visit(node)
        self._classes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute):
            self.generic_visit(node)
            return
        if node.func.attr == "delete" and _receiver_is_store_like(node.func.value):
            self._record(node, "store-like .delete() is a forbidden key-only surface")
        if node.func.attr == "delete_object":
            allowed_location = self._classes[-1:] == ["ObjectStore"] and self._functions[-1:] == [
                "delete_version"
            ]
            has_version_id = any(keyword.arg == "VersionId" for keyword in node.keywords)
            if not (allowed_location and has_version_id):
                self._record(
                    node,
                    "raw delete_object is allowed only in ObjectStore.delete_version "
                    "with VersionId",
                )
        self.generic_visit(node)

    def _record(self, node: ast.Call, reason: str) -> None:
        self.violations.append(f"{self._path}:{node.lineno}: {reason}")


def _violations(source: str, path: Path = Path("fixture.py")) -> list[str]:
    visitor = _DeleteVisitor(path)
    visitor.visit(ast.parse(source, filename=str(path)))
    return visitor.violations


def test_production_object_store_deletes_are_versioned() -> None:
    violations: list[str] = []
    for path in sorted((_ROOT / "src" / "kdive").rglob("*.py")):
        violations.extend(_violations(path.read_text(), path.relative_to(_ROOT)))
    assert not violations, "\n".join(violations)


def test_guard_rejects_key_only_store_delete_and_raw_delete_object() -> None:
    assert len(_violations('store.delete("key")')) == 1
    assert len(_violations('self.object_store.delete("key")')) == 1
    assert len(_violations('self._store_client.delete("key")')) == 1
    assert len(_violations('client.delete_object(Bucket="b", Key="key")')) == 1


def test_guard_rejects_raw_version_delete_outside_the_store_boundary() -> None:
    source = 'client.delete_object(Bucket="b", Key="key", VersionId="v1")'
    assert len(_violations(source)) == 1


def test_guard_allows_only_the_versioned_raw_store_boundary() -> None:
    source = """
class ObjectStore:
    def delete_version(self, key, version_id):
        self._client.delete_object(Bucket=self._bucket, Key=key, VersionId=version_id)
"""
    assert _violations(source) == []


def test_guard_does_not_reject_unrelated_delete_methods() -> None:
    source = """
SNAPSHOTS.delete(conn, snapshot_id)
volume.delete(0)
snapshotter.delete(domain_name, snapshot_name)
"""
    assert _violations(source) == []
